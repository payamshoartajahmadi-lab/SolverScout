"""Assemble existing offline evidence only; never import or execute the product."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from source_hashes import runtime_manifest, harness_manifest

B = Path(__file__).resolve().parent
ROOT = B.parents[2]
BASE = '37cd0803b1244320d1ab4866dd303fd7ba2a6780'
TARGET_NAMES = [
    'test_explainer_receives_full_tie_or_insufficient_evidence[identifier_tie]',
    'test_exe_009_010_supervisor_reaps_hung_objective[False]',
    'test_exe_009_010_supervisor_reaps_hung_objective[True]',
]
STARTUP_NAMES = [
    'test_exe_009_startup_timeout_keeps_zero_evaluations_and_correct_history[blocked_startup]',
    'test_exe_009_startup_timeout_keeps_zero_evaluations_and_correct_history[late_identity_ack]',
]

def read(path):
    return json.loads((B / path).read_text(encoding='utf-8'))

def save(path, value):
    (B / path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')

def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()

def main():
    assert not (B / 'SolverScout_Offline_Acceptance_Evidence.zip').exists(), 'Preserve an already-built archive'
    runs = [json.loads(p.read_text(encoding='utf-8')) for p in (B / 'offline').glob('*-result.json')]
    runs.sort(key=lambda r: r['started_at_utc'])
    by_label = {r['run_label']: r for r in runs}
    final_label = sys.argv[1] if len(sys.argv) > 1 else 'full-final-01'
    final = by_label[final_label]
    positive = [by_label['targeted-0' + str(n)] for n in (1, 2, 3)] + [final]
    checks = []
    for r in positive:
        cases = list(ET.parse(B / r['junit']).getroot().iter('testcase'))
        target_outcomes = {}
        for name in TARGET_NAMES + STARTUP_NAMES:
            matched = [c for c in cases if c.get('name') == name]
            target_outcomes[name] = len(matched) == 1 and not any(matched[0].find(t) is not None for t in ('failure', 'error', 'skipped'))
        repairs = [c for c in cases if c.get('classname') == 'tests.test_narrative_contract_repair']
        checks.append({'run_label': r['run_label'], 'target_pass': target_outcomes,
                       'repair_tests': len(repairs), 'repair_all_pass': len(repairs) == 31 and all(
                           not any(c.find(t) is not None for t in ('failure', 'error', 'skipped')) for c in repairs)})
    source, harness = runtime_manifest(), harness_manifest()
    guards = {r['run_label']: read(r['guard_report']) for r in positive}
    guard_checks = {label: (
        g['blocked_non_loopback_connection_attempts'] == 0
        and g['live_provider_test_selected'] is False and g['real_model_calls_authorized'] == 0
        and Path(g['application_import_path']).resolve() == (ROOT/'src/p_mc_optimizer/__init__.py').resolve()
    ) for label, g in guards.items()}
    captured_files = []
    for r in positive:
        for kind in ('tie', 'numeric'):
            folder = B / (kind + '-' + r['run_label'])
            index = json.loads((folder/'evidence_index.json').read_text(encoding='utf-8'))
            assert index['run_label'] == r['run_label']
            if kind == 'numeric':
                assert sum(item['path'].startswith('traces/') for item in index['files']) == 4
            for item in index['files']:
                body = (folder/item['path']).read_bytes()
                assert len(body) == item['size_bytes'] and sha256(body).hexdigest() == item['sha256']
                captured_files.append((folder/item['path']).relative_to(B).as_posix())
    tracked_clean = not git('status','--porcelain','--','src','tests','schemas','scripts','pyproject.toml')
    application_unchanged = git('rev-parse', BASE+':src') == git('rev-parse', 'HEAD:src') and source['canonical_source_sha256'] == by_label['reproduce-04']['source_sha256']
    unchanged = all(r['source_bytes_unchanged_during_run'] and r['harness_bytes_unchanged_during_run']
                    and r['source_sha256'] == source['canonical_source_sha256']
                    and r['harness_sha256'] == harness['canonical_harness_sha256'] for r in positive)
    passed = unchanged and all(r['exit_code'] == 0 and r['junit_counts']['passed'] == r['junit_counts']['tests'] for r in positive)
    passed = passed and all(all(c['target_pass'].values()) and c['repair_all_pass'] for c in checks)
    passed = passed and all(guard_checks.values()) and tracked_clean and not final['source_changes_at_start']
    status = 'OFFLINE_PASS' if passed else 'OFFLINE_NOT_ACCEPTED'
    assert final['head_sha'] == git('rev-parse', 'HEAD'), 'Report must bind to the actual final tested source commit'
    summary = {
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'status': status, 'live_acceptance': 'NOT_RUN', 'delivery_readiness': 'NOT_VERIFIED',
        'current_test_execution': 'EXECUTED_OFFLINE_ONLY', 'new_model_calls': 0,
        'new_live_provider_calls': 0, 'new_objective_evaluations_outside_tests': 0,
        'new_solver_calls_outside_tests': 0,
        'numeric_activity_inside_tests': 'Executed by necessary offline tests; aggregate not measured and not asserted zero',
        'branch': git('branch', '--show-current'), 'reproduced_commit': BASE,
        'final_tested_commit': final['head_sha'], 'baseline': read('baseline.json'),
        'final_run_label': final_label,
        'environment': read('environment.json'), 'source_sha256': source['canonical_source_sha256'],
        'harness_sha256': harness['canonical_harness_sha256'], 'all_final_run_bytes_match_current': unchanged,
        'application_code_changed_this_mission': not application_unchanged,
        'final_tracked_source_clean': tracked_clean,
        'final_offline_guard_checks': guard_checks,
        'captured_evidence_hashes_verified': captured_files,
        'changed_source_paths': git('diff', '--name-only', BASE, 'HEAD').splitlines(),
        'fixture_timing_seconds': {'identifier_tie': {'before': 45, 'after': 300},
                                  'insufficient_evidence': {'before': 45, 'after': 45},
                                  'hung_objective_total': {'before': 1.8, 'after': 10},
                                  'new_startup_controls_total': 5, 'unchanged_overshoot_upper_bound': 0.75},
        'acceptance_checks': checks, 'runs': runs,
        'historical_result_preserved': '798 passed / 3 failed in prior full-final-02; historical reports not rewritten',
        'full_run_environment_verification': read('full-run-environment-verification.json'),
        'diagnosis': read('diagnosis-before-fix.json'),
        'limitations': [
            'reproduce-01 is a basetemp-parent setup error; reproduce-02 identifier case is a Windows path failure, not the workflow diagnosis',
            'Fresh reproduce-03/04 and numeric-probe-01 are separate runs; exact failed reproduce-03 True timeline was not captured',
            'Instrumented numeric-probe-01 True passed near the deadline; it is not mislabeled FAIL or a fix',
            'Fixture synchronization and observation add overhead; no causal OS or drive latency claim',
            'Offline network guard covers pytest process/threads, not a machine-wide independent traffic audit',
            'Stored explainer contexts are actual gateway records, not independently captured HTTP wire bytes',
            'Historical C-L1 raw artifacts remain NOT_FOUND in the previously examined scope; synthetic tests are not live evidence',
        ],
        'live_prerequisites': 'Next_Live_Prerequisites_FA.md',
        'evidence_index': 'evidence_index.json', 'archive': 'SolverScout_Offline_Acceptance_Evidence.zip',
        'publication_note': 'Source commits precede final full suite. This report/evidence is committed separately; final publication SHA is reported after push verification.',
    }
    save('summary.json', summary)
    save('source_manifest.json', {**source, 'tested_commit': final['head_sha']})
    save('harness_manifest.json', {**harness, 'tested_commit': final['head_sha']})
    patch = subprocess.check_output(['git', 'diff', BASE, 'HEAD', '--', 'tests'], cwd=ROOT)
    (B / 'offline-fixtures.patch').write_bytes(patch)
    rows = '\n'.join(f"| `{r['run_label']}` | {r['junit_counts']['passed']} | {r['junit_counts']['failures']} | {r['junit_counts']['errors']} | {r['junit_counts']['skipped']} | {r['elapsed_seconds']:.3f} |" for r in runs)
    full_attempts = [r for r in runs if r['run_label'].startswith('full-final-')]
    full_note = ('مجموعهٔ کامل یک بار اجرا شد.' if len(full_attempts) == 1 else
                 'تعداد اجرای کامل: '+str(len(full_attempts))+'؛ همهٔ نتایج حفظ شده‌اند. اجرای اول ۷۹۳ موفق، ۳ شکست و ۷ خطای setup داشت. مسیر موقت انتخاب‌شده باعث رسیدن فایل‌های qualification به طول ۲۶۱ نویسه و FileNotFoundError شد؛ لاگ، خطاهای مستقیم و شکست‌های وابسته را ثبت کرده است. در اجرای دوم فقط basetemp کوتاه شد؛ هر ۱۰ مورد قبلی و کل مجموعه با همان SHA و hash موفق شدند. تشخیص پیش از تکرار در `full-run-environment-diagnosis.json` و تأیید پس از اجرا در `full-run-environment-verification.json` حفظ شده‌اند. این اصلاح محیط اجرا، افزودن پشتیبانی عمومی مسیر بلند به محصول نیست.')
    report = f'''# گزارش پذیرش آفلاین SolverScout

وضعیت این نسخه: **{status} / LIVE_NOT_RUN**. پذیرش زنده انجام نشده و آمادگی تحویل `NOT_VERIFIED` است. نتیجهٔ تاریخی FAIL و همهٔ بازتولیدهای ناموفق حفظ شده‌اند.

## نسخه و محیط

- شاخه: `fix/narrative-contract-repair`؛ worktree: `{ROOT}`.
- SHA بازتولید: `{BASE}`؛ SHA اجرای کامل نهایی: `{final['head_sha']}`.
- مبنای آزمون قبلی `d32691dfbb7e5f9faeafe5dcb119d65e97d46de3` بود؛ تا `37cd080` فقط ۵۵ فایل گزارش و شواهد افزوده شده بود.
- بررسی مستقیم origin در شروع، dev را روی `e151aecc0a79a7e6b421b07934df6977bc9068c7` نشان داد؛ این نسخه از dev قبلاً در ancestry نامزد بود. remote-tracking قدیمی مبنای ادعا قرار نگرفت. dev/main مقصد ادغام یا push این مأموریت نیستند.
- Python 3.12.3 روی Windows؛ نسخهٔ وابستگی‌ها در result هر اجرا و مشخصات سیستم در `environment.json` ثبت شده است. وابستگی تازه‌ای نصب نشد. import صریحاً از `src` همین worktree انجام شد.
- hash کد محصول `{source['canonical_source_sha256']}` و hash harness نهایی `{harness['canonical_harness_sha256']}` است. هر سه تکرار و اجرای کامل به همین bytes متصل‌اند: `{unchanged}`. سه تکرار پیش از commit، با ثبت dirty paths و manifest کامل انجام شدند؛ اجرای کامل روی SHA commitشده بود.

## علت‌های مستند و اصلاح

در `reproduce-03` هر سه مورد هدف شکست خوردند. گزارش واقعی تساوی `time_budget`، `selection_status=not_reached`، تصمیم null و صفر پایلوت داشت؛ اولین explainer پاسخ mock مخصوص advisor را دریافت کرد و repair ادعای انتخابِ انجام‌نشده را رد کرد. نتیجهٔ همان اجرا `schema_fallback` است. در `reproduce-04` روی TEMP معمول Windows، شش پایلوت کامل مجموعاً ۲۸٫۲۶۴ ثانیه مصرف کردند؛ پایلوت هفتم فقط ۰٫۴۰۸ ثانیه مهلت داشت و بدون ارزیابی timeout شد. تصمیم null و نتیجهٔ توضیح `provider_error` بود. این دو مسیر جداگانه گزارش شده‌اند؛ گزارش job و contextهای ذخیره‌شده در `tie-reproduce-03/` و `tie-reproduce-04/` موجودند.

علت fixture مثبت تساوی، مهلت ۴۵ ثانیه‌ای با ۱۰ ثانیه ذخیرهٔ verification بود که تکمیل ۹ پایلوت را تضمین نمی‌کرد. سقف فقط همین fixture از **۴۵ به ۳۰۰ ثانیه** تغییر کرد؛ آزمون مثبت مشابه همین فایل، برای همان ۵۵۰۲ FE و ۹ پایلوت از قبل سقف ۳۰۰ داشت. شرط شاخهٔ insufficient_evidence همچنان ۴۵ است. اکنون آزمون دقیقاً ۹ پایلوت کاملِ ۵۰۰ ارزیابی، سه روش/سه تکرار، انتخاب واقعی، معیار تعیین‌کنندهٔ `algorithm_id` و سازگاری توضیح با همان تصمیم را کنترل می‌کند. بودجه FE، seed، تعداد پایلوت، صف پاسخ mock، validator و سیاست پیش‌فرض محصول تغییر نکردند.

در fixture عددی قبلی، مهلت کل **۱٫۸ ثانیه** با زمان startup هم‌پوشانی داشت. در probe تازهٔ حالت False، identity در ۱٫۸۱۲ ثانیه دریافت شد؛ هیچ start پذیرفته یا objective وارد نشده بود و شمارنده‌ها ۰/۰/۰ با تاریخچهٔ خالیِ کامل بودند. پس الزام fixture به history ناقص صحیح نبود. در حالت True، بازتولید بدون ابزار `reproduce-03` به جای cancelled مقدار timed_out ثبت کرد؛ probe جداگانهٔ True موفق شد، اما ورود تابع در ۱٫۷۶۵ و مشاهدهٔ لغو در ۱٫۷۹۷ ثانیه، حدود ۳ میلی‌ثانیه پیش از deadline بود. timeline دقیق اجرای ناموفق True در reproduce-03 موجود نیست؛ آن را با timeline probe موفق جایگزین نکرده‌ایم. موفقیت هر دو numeric در reproduce-04 نیز رفع مشکل تلقی نشد.

اصلاح عددی فقط fixture است: deadline کل **۱۰ ثانیه**، Event ورود واقعی تابع، مشاهدهٔ ACK پذیرفته‌شدن start و تطبیق PID و creation time پیش از لغو. برای timeout، تابع واردشده تا همان deadline اصلی معلق می‌ماند. دو کنترل startup با deadline کل **۵ ثانیه** افزوده شدند: توقف پیش از identity بدون done، و identity ACK عمداً دیرهنگام که done واقعی ۰/۰/۰ می‌دهد. اولی history ناقص و دومی history خالی کامل دارد؛ هر دو objective را آغاز نمی‌کنند. fixture دوم صریحاً مصنوعی است و حداکثر ۰٫۲۵ ثانیه برای آماده‌شدن پیام پایان صبر می‌کند. ساعت محصول قبل از startup آغاز می‌شود؛ بازنشانی نشده و شرط `overshoot < 0.75` حفظ شده است.

برای هر چهار مسیر، واقعی‌بودن توقف child با وضعیت multiprocessing و بررسی همان PID/creation time در سیستم‌عامل اثبات می‌شود. وجود done در حالت late ACK به معنی خروج طبیعی فرایند نیست؛ trace نخست exitcode برابر -15 ثبت کرده است. شمارنده‌های تابع معلق ۱/۰/۰ با history ناقص‌اند. نقصی در کد محصول از این شواهد اثبات نشد و هیچ فایل محصول تغییر نکرد.

## تمام اجراهای این مأموریت

| اجرا | PASS | FAIL | ERROR | SKIP | زمان runner، ثانیه |
|---|---:|---:|---:|---:|---:|
{rows}

`reproduce-01` خطای آماده‌سازی parent مسیر basetemp است؛ آزمون‌های هدف به بدنه نرسیدند. در `reproduce-02` شکست identifier ناشی از مسیر بلند Windows بود. این دو اجرا حفظ شده‌اند و مبنای تشخیص workflow نیستند. دربارهٔ تفاوت سرعت G و C ادعای علت سخت‌افزاری نشده است.

هر سه سناریوی هدف در سه اجرای پیاپی هدفمند موفق شدند؛ هر اجرای هدفمند شامل ۳۱ آزمون repair، کنترل عدم انتخاب، دو کنترل startup، crash/accounting و quota بود. {full_note} نتیجهٔ اجرای کامل نهایی `{final_label}` روی نسخهٔ ثابت: **{final['junit_counts']['passed']} PASS، {final['junit_counts']['failures']} FAIL، {final['junit_counts']['errors']} ERROR، {final['junit_counts']['skipped']} SKIP**؛ {final['deselected']} تست live با marker کنار گذاشته شد. skip یا xfail تازه‌ای اضافه نشد. نتیجهٔ هر testcase در JUnit و progress ذخیره شده است.

## شواهد، مصرف و محدودیت

`offline/` شامل همهٔ log/JUnit/result/manifestها و timeline probe پیش از اصلاح است. پوشه‌های `tie-*` گزارش واقعی ذخیره‌شده و درخواست/پاسخ context explainer را دارند؛ این داده‌ها مصنوعیِ آزمون آفلاین هستند و transcript مستقل شبکه یا پاسخ مدل زنده نیستند. پوشه‌های `numeric-*` چهار trace واقعی fixture در هر اجرای نهایی را نگه می‌دارند. `diagnosis-before-fix.json`، `numeric-review.json`، patch و manifestها ارتباط علت، تغییر و نسخه را مشخص می‌کنند. index برای هر فایل مسیر، اندازه، SHA-256 و ارتباط اجرا/نسخه دارد.

تماس واقعی مدل **۰**؛ پذیرش live و connection test واقعی اجرا نشده‌اند. ارزیابی تابع و اجرای حل‌گر صرفاً داخل آزمون‌های لازم انجام شده است؛ مجموع آن‌ها اندازه‌گیری نشده و صفر اعلام نمی‌شود. خارج آزمون‌ها هر دو شمارنده **۰** هستند. guard، اتصال غیر-loopback در pytest و threadهایش را منع کرده؛ این شاهد audit مستقل کل ترافیک سیستم یا تمام childها نیست.

هیچ دیتابیس، `.env`، credential، cookie، header اتصال، `_private_qa`، `_private_test_state` یا temp pytest در ZIP نیست. فقط کپی‌های عمومی منتخب بسته‌بندی می‌شوند؛ `numeric_execution.authorization` عمومی حذف نشده است. آرشیو و اسناد تاریخی، تغییرات ذخیره‌شدهٔ کاربر و stash حفظ شده‌اند؛ `preservation-check.json` تطبیق آن‌ها را ثبت می‌کند.

شواهد خام تاریخی C-L1 در محدودهٔ قبلی بازیابی نشده‌اند؛ این اجراها آن‌ها را بازسازی نمی‌کنند. برای مرحلهٔ بعد، گزارش واقعی E1 از سیستم قبلی، آماده‌سازی و auditهای UI/کنترل‌های C-A1/C-A2 روی همین نسخه، profile معتبر، freeze و ledger تازه و مجوز جداگانهٔ پذیرش زنده لازم است. جزئیات در `Next_Live_Prerequisites_FA.md` است. موفقیت آفلاین به معنی پذیرش نهایی تحویل نیست.

گزارش و شواهد پس از commitهای fixture در commit جداگانه روی همین شاخه منتشر می‌شوند؛ SHA انتشار پس از push خواندنی تأیید و در پاسخ نهایی اعلام می‌شود. هیچ merge به dev/main انجام نمی‌شود.
'''
    (B / 'Offline_Acceptance_Status_FA.md').write_text(report, encoding='utf-8')
    print(json.dumps({'status': status, 'tested_commit': final['head_sha'], 'full_counts': final['junit_counts'], 'checks': checks}, ensure_ascii=True))

if __name__ == '__main__':
    main()
