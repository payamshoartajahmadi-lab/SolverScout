"""Compose reports from captured results; never execute tests or application code."""
from datetime import datetime, timezone
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent


def read(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))


baseline = read('baseline.json')
provenance = read('git-provenance.json')
source = read('source_manifest.json')
harness = read('harness_manifest.json')
remote = read('remote-before-publication.json')
sample = read('synthetic-repair-example.json')
invalid_sample = sample['repair']['failed_sections'][0]
safe_error_example = json.dumps({
    'evidence_kind': sample['evidence_kind'],
    'section_id': invalid_sample['section_id'],
    'paragraph_id': invalid_sample['paragraphs'][0]['paragraph_id'],
    'structural_error': invalid_sample['structural_errors'][0],
}, ensure_ascii=False, indent=2)
runs = sorted([read(p.relative_to(ROOT).as_posix()) for p in (ROOT / 'offline').glob('*-result.json')],
              key=lambda run: run['started_at_utc'])
full = [run for run in runs if run['run_label'].startswith('full-final-')][-1]
targeted = next(run for run in runs if run['run_label'] == 'targeted-final-02')
for run in runs:
    cases = list(ET.parse(ROOT / ('offline/' + run['run_label'] + '.xml')).iter('testcase'))
    run['failed_testcases'] = [node.get('classname', '') + '::' + node.get('name', '') for node in cases
                              if node.find('failure') is not None or node.find('error') is not None]
    run['failure_messages'] = [
        {'testcase': node.get('classname', '') + '::' + node.get('name', ''),
         'first_line': item.get('message', '').splitlines()[0] if item.get('message') else ''}
        for node in cases for tag in ('failure', 'error') for item in node.findall(tag)
    ]
passed = all(run['exit_code'] == 0 and run['junit_counts']['tests'] > 0
             and not any(run['junit_counts'][k] for k in ('failures', 'errors', 'skipped'))
             for run in (targeted, full))
contract_cases = [node for node in ET.parse(ROOT / ('offline/' + full['run_label'] + '.xml')).iter('testcase')
                  if node.get('classname', '').endswith('test_narrative_contract_repair')]
contract_passed = sum(not any(node.find(k) is not None for k in ('failure', 'error', 'skipped'))
                      for node in contract_cases)
guard = [read('offline/' + run['run_label'] + '-network-guard.json') for run in runs]
summary = {
    'schema_version': 'solverscout-contract-repair-status/1.0',
    'created_at_utc': datetime.now(timezone.utc).isoformat(),
    'branch': baseline['branch'], 'worktree': baseline['worktree'],
    'baseline': baseline, 'tested_source_commit': provenance['tested_commit'],
    'remote_observation_before_publication': remote,
    'implementation_commit': provenance['implementation_commit'],
    'runtime_source_sha256': source['canonical_source_sha256'],
    'harness_sha256': harness['canonical_harness_sha256'],
    'source_hash_semantics': 'Existing closure harness definition over working-file bytes; Git blob hashes are separately recorded',
    'source_after_full_test': 'UNCHANGED; later commit contains reports and evidence only',
    'offline_repair_and_regression_status': 'PASS' if passed else 'FAIL',
    'new_contract_regression_cases': {'total': len(contract_cases), 'passed': contract_passed},
    'test_runs': runs,
    'earlier_failures': {
        'targeted-new-01': '24 passed, 2 failed: corrective guidance omitted the actual missing problem.sense token; fixed without relaxing assertions',
        'targeted-final-01': '241 passed, 15 failed: 14 missing-protego import failures and one API fixture startup timeout; installed only declared missing protego/pypdf versions; unchanged source passed rerun',
    },
    'new_live_acceptance': 'NOT_RUN', 'final_delivery_readiness': 'NOT_VERIFIED',
    'live_scenarios': {name: 'NOT_RUN' for name in ('C-A2', 'C-A1', 'C-L1', 'C-L4', 'C-L2', 'C-L3', 'C-E1-R')},
    'new_model_calls': 0, 'new_real_model_tokens': 0,
    'new_objective_evaluations_outside_required_tests': 0,
    'new_solver_calls_outside_required_tests': 0,
    'objective_evaluations_within_offline_tests': 'Executed by required existing tests; aggregate not instrumented',
    'solver_calls_within_offline_tests': 'Executed by required existing tests; aggregate not instrumented',
    'network_guards': guard,
    'network_guard_limit': 'Socket guard covers pytest process and threads; numerical child processes use existing application isolation. Not a machine-wide network audit.',
    'new_fixtures': 'SYNTHETIC_NOT_HISTORICAL; no recovered historical C-L1 raw response claimed',
    'historical_closure': {'status': 'FAIL according to recorded Markdown', 'raw_C_L1_artifacts': 'NOT_FOUND in prior recovery scope; not reconstructed', 'historical_reports_modified': False},
    'windows_and_closure_commits_included': provenance['required_commits_are_ancestors'],
    'dev_main_modified': False, 'new_code_scope': 'Narrative prompt, typed repair snapshot, safe structural diagnostics, orchestrator propagation, regression tests, LF schema checkout compatibility',
    'dependency_setup': read('dependency-setup.json'),
    'schema_eol_diagnosis': read('schema-eol-diagnosis.json'),
    'schema_eol_compatibility_committed': '.gitattributes' in provenance['changed_paths_since_integration'],
    'full_suite_failure_diagnostics': ['schema-eol-diagnosis.json', 'numeric-startup-diagnosis.json',
                                       'offline/legacy-identifier-tie-diagnostic.json'],
    'remaining_full_suite_failures': full['failed_testcases'],
    'latest_failure_cause_limit': 'Latest JUnit proves assertion mismatches and exhausted mock response queue. The detailed source diagnoses refer only to full-final-01; the cause of each latest failure is not independently established.',
    'remaining': (['Recorded full-suite failures must be resolved within an explicitly agreed scope; no budgets or assertions weakened']
                  if full['exit_code'] != 0 or full['failed_testcases'] else []) +
                 ['Real archived E1 report', 'Genuine same-source UI and adversarial audits', 'Fresh campaign freeze and separately authorized live acceptance', 'Final delivery review'],
    'next_live_instructions': 'Next_Live_Acceptance_FA.md',
    'publication': {'remote': 'origin', 'branch': baseline['branch'], 'force': False,
                    'note': 'Evidence is prepared before its containing commit. Final push outcome and remote SHA are verified separately and reported in the final response.'},
}
(ROOT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
rows = []
for run in runs:
    counts = run['junit_counts']
    rows.append('| ' + ' | '.join(str(v) for v in (run['run_label'], counts['tests'], counts['passed'],
                counts['failures'], counts['errors'], counts['skipped'], run.get('deselected', 0), run['exit_code'])) + ' |')
table = '\n'.join(rows)
final_failures_markdown = ('\n'.join('- `' + name + '`' for name in full['failed_testcases'])
                           or 'هیچ failure یا error در JUnit آخرین اجرای کامل ثبت نشده است.')
report = f'''# گزارش اصلاح قرارداد و repair گزارش‌نویسی SolverScout

نتیجهٔ اصلاح و آزمون آفلاین: **{summary['offline_repair_and_regression_status']}**. پذیرش زندهٔ نسخهٔ جدید: **NOT_RUN**. آمادگی تحویل نهایی: **NOT_VERIFIED — هنوز تأیید نشده**.

نسخهٔ آزموده‌شده `{provenance['tested_commit']}` در شاخهٔ `{baseline['branch']}` است. اجرای کامل آفلاین {full['junit_counts']['passed']} مورد موفق، {full['junit_counts']['failures']} شکست، {full['junit_counts']['errors']} خطای اجرا، {full['junit_counts']['skipped']} skipped و {full.get('deselected', 0)} deselected داشت؛ exit code برابر {full['exit_code']} بود. {contract_passed} مورد از {len(contract_cases)} آزمون جدید repair نیز موفق بود. این نتیجه پذیرش زنده یا تأیید کیفیت مدل نیست.

این گزارش پیش از commit حامل شواهد تهیه شده است. آن commit فقط گزارش و شواهد را اضافه می‌کند؛ کد آزموده‌شده تغییر نمی‌کند. وضعیت واقعی push و SHA نهایی remote جداگانه پس از ارسال در پاسخ پایان اعلام می‌شود.

## Git و نسخهٔ مبنا

| مرجع | SHA کامل |
|---|---|
| dev محلی و HEAD اولیهٔ worktree اصلی | `{baseline['local_dev']}` |
| origin/dev پس از fetch موفق | `{baseline['fetch']['remote_dev']}` |
| origin/fix/narrative-v3-closure پس از fetch | `{baseline['fetch']['remote_closure']}` |
| main محلی | `{baseline['local_main']}` |
| commit ادغام در نامزد جدید | `{baseline['integration_commit']}` |
| commit پیاده‌سازی repair و آزمون‌های جدید | `{provenance['implementation_commit']}` |
| commit کد و آزمون جدید؛ مبنای اجرای کامل | `{provenance['tested_commit']}` |

dev در آغاز نسبت به origin/dev سه commit جلو و صفر commit عقب بود؛ تغییر تازه‌ای در origin/dev برای ادغام وجود نداشت. تغییر ثبت‌نشدهٔ tracked در آغاز نبود. worktree جداگانه در `{baseline['worktree']}` ساخته شد و closure بدون تعارض در همین شاخه ادغام شد. dev و main مقصد ادغام یا push این مأموریت نیستند و HEAD آن‌ها حفظ شده است.

مشاهدهٔ مستقیم remote پیش از انتشار در `{remote['checked_at_utc']}` با `git ls-remote` در `remote-before-publication.json` ثبت شده است؛ این مدرک مستقل از remote-tracking محلی است. نتیجهٔ push شاخهٔ جدید پس از ساخت commit شواهد بررسی می‌شود.

اصلاحات Windows در commitهای `5ddee26ed86cefe9585e3f89fe02fc7a9f4f7816` و `13184acb677a2dbe96524380057251027e73db41` و اصلاحات/اسناد closure در `b4723bf56d4ea76a0aec16f994ae87fa0e4b2713` و `ad449d252f243ff0400e3d42caae5d25defe5b11` همگی ancestor نسخهٔ آزموده‌شده هستند؛ مدرک در `git-provenance.json` است. هفت فایل پشتیبان و چهار فایل بستهٔ بازیابی قبلی حفظ شده‌اند. `preservation-check.json` hash فعلی، تطبیق سه کپی تست با رکورد preservation و تطبیق ZIP بازیابی با hash ثبت‌شده را دارد.

## علت تأییدشده و اصلاح انجام‌شده

در کد قبل از patch جدید، `provider_schema` عمداً محدودیت `pattern` را از schema ارسالی حذف می‌کرد. اعتبارسنج داخلی همچنان شناسهٔ دارای خط تیره را رد می‌کرد، اما خطای section به SCHEMA_INVALID عمومی تقلیل می‌یافت. orchestrator برای فرستادن همان section به repair دوباره `NarrativeSectionV3` را می‌ساخت و با همان خطا آن را کنار می‌گذاشت؛ بنابراین دادهٔ لازم از زمینهٔ repair حذف می‌شد.

این مسیر خرابی با خواندن کد و آزمون مصنوعی تأیید شده است. تطبیق مستقل آن با پاسخ خام تاریخی C-L1، به‌دلیل نبود پاسخ خام روی این سیستم، `NOT_VERIFIED` باقی می‌ماند.

در commit جدید:

- prompt نسخهٔ ۳٫۱، regex دقیق `^[a-z][a-z0-9_]{{0,47}}$`، نمونهٔ معتبر `problem_main`، نامعتبر بودن `problem-main` و یکتایی نسبت به بخش‌های پذیرفته‌شده را برای draft و repair صریح بیان می‌کند. شناسه به‌صورت خاموش تغییر داده نمی‌شود.
- قرارداد بسته و قابل‌اعتبارسنجی `InvalidSectionRepairV1` به فهرست موجود `failed_sections` افزوده شده است: حداکثر سه پاراگراف، دوازده خطای ساختاری و ۱۶۳۸۴ بایت برای هر snapshot. متن/ارجاع‌های لازم با سقف طول حفظ می‌شوند؛ تغییر یا حذف از snapshot با `truncated` مشخص است و raw اصلی تغییر نمی‌کند.
- خطا دارای section، مسیر دقیق فیلد، نوع خطا، قاعده و راهنمای فارسی است. Pydantic input/msg/ctx و فیلدهای اضافی منتقل نمی‌شوند. اطلاعات credential، header و مسیر خصوصی از رشته‌های snapshot حذف می‌شوند؛ نام عمومی `numeric_execution.authorization` کورکورانه حذف نمی‌شود.
- orchestrator همان snapshot را به repair می‌رساند و آن را متن معتبر یا پذیرفته‌شده نمی‌شمارد. request hash شامل snapshot است؛ شکل ریشهٔ context و قرارداد خروجی writer/reviewer ثابت مانده‌اند و contextهای قدیمی با hash قبلی roundtrip می‌شوند.
- الزام فیزیکی توکن `{{{{fact:problem.sense}}}}` و برابری توکن‌های متن با `fact_refs` حفظ شده است. راهنمای MISSING_REQUIRED_FACT اکنون خودِ توکن‌های غایب را نام می‌برد. اصلاح شناسه، خطای binding را رفع‌شده فرض نمی‌کند.

`models.py` و `provider.py` تغییر نکرده‌اند؛ آزمون‌ها بدنهٔ واقعی ساخته‌شده توسط `build_request` و `prepare_provider_request` را در هر سه حالت پاسخ بررسی می‌کنند. فرض پشتیبانی provider از pattern اضافه نشده است. patch جدید منطق انتخاب روش، حل‌گرها، seedها، بودجه‌ها، آستانه‌های عددی یا معیارهای پذیرش قبلی را تغییر نمی‌دهد. نسخهٔ evidence policy افزایش نیافته؛ hash prompt جدید طبیعتاً تغییر می‌کند و گزارش‌های ذخیره‌شدهٔ قدیمی همچنان خوانده می‌شوند.

## آزمون‌های واقعی این مأموریت

| اجرا | کل | passed | failed | errors | skipped | deselected | exit |
|---|---:|---:|---:|---:|---:|---:|---:|
{table}

شکست‌های ثبت‌شده در آخرین اجرای کامل:

{final_failures_markdown}

اجرای اول دو شکست واقعی در راهنمای `problem.sense` نشان داد؛ راهنما اصلاح شد و assertionها ضعیف نشدند. اجرای گستردهٔ اول ۱۴ شکست import مربوط به نبود `protego` و یک timeout راه‌اندازی fixture محلی API داشت. بررسی وابستگی‌های ثبت‌شده نشان داد `protego==0.7.0` و `pypdf==7.0.0` نصب نبودند. فقط همین دو وابستگی با `--no-deps` به محیط موجود اضافه شدند؛ نسخهٔ وابستگی‌های دیگر، specification پروژه یا معیارهای تست تغییر نکرد. اجرای بعدیِ همان کد تمام ۲۵۶ آزمون را گذراند. این شکست‌ها و لاگ‌هایشان حفظ شده‌اند.

در نخستین اجرای کامل، آزمون byte-exact مربوط به trial schemaها نیز بررسی شد. `schema-eol-diagnosis.json` نشان می‌دهد فایل v1 دقیقاً ۱۳۲ و فایل v2 دقیقاً ۱۷۹ بایت CR ناشی از checkout ویندوز داشتند؛ تبدیل CRLF به LF در حافظه هر دو را دقیقاً برابر خروجی فعلی مدل کرد. وضعیت ثبت اصلاح پایان‌خط در `.gitattributes`: `{summary['schema_eol_compatibility_committed']}`. این اصلاح محتوا یا assertion را تغییر نمی‌دهد و schemaها دوباره تولید نشده‌اند. هر اجرای کامل و نام testcaseهای شکست‌خوردهٔ آن در summary و JUnit همان اجرا محفوظ است.

در مقایسهٔ تکمیلی، یک اختلاف قدیمی نامرتبط در `execution_result.v1.json` دیده شد: مدل از commit تاریخی `964651875a96b7808c268a5d519df6b56a81017f` فیلد اختیاری `runtime_provenance` دارد، ولی JSON ثبت‌شده آن را ندارد. این مورد خارج دامنهٔ repair باقی ماند و ادعای رفع آن نشده است؛ موفقیت pytest به معنی تطبیق همهٔ schemaهای static نیست.

سه شکست دیگر نخستین اجرای کامل عمداً با تغییر بودجه یا معیار مخفی نشدند. در `test_explainer_receives_full_tie_or_insufficient_evidence[identifier_tie]`، رکورد واقعیِ آزمون مصنوعی نشان داد بودجهٔ ۴۵ ثانیه‌ای پس از شش pilot کامل رو به اتمام بود؛ نخستین pilot مربوط به PSO فقط ۰٫۵۱۷ ثانیه مهلت داشت و با صفر ارزیابی timeout شد. انتخاب `not_reached` و decision تهی بود؛ mock با این وجود ادعای شکستن تساوی داشت. اعتبارسنج درست خطای `selection_explanation_fa: no selection was made` داد و repair بعدی صف mock را تمام کرد. پروژکشن امن این رکوردها در `offline/legacy-identifier-tie-diagnostic.json` است؛ هیچ دیتابیس یا credential صادر نشده است.

در دو مورد `test_exe_009_010_supervisor_reaps_hung_objective`، fixture مهلت کل ۱٫۸ ثانیه دارد و deadline پیش از spawn شروع می‌شود. برای حالت False، JUnit صفر ارزیابی و نبود runtime identity را نشان می‌دهد؛ برای True، فقط `timed_out` به‌جای `cancelled` در شکست ثبت شده است. این مشاهدات با تمام‌شدن مهلت پیش از رسیدن به تابع معلق سازگارند؛ عامل دقیق کندی و انتساب آن به patch گزارش‌نویسی `NOT_VERIFIED` است. موفقیت reaping یا cancellation از این نتایج استنتاج نمی‌شود. کد عددی و assertionهای این آزمون‌ها تغییر نکرده‌اند؛ `numeric-startup-diagnosis.json` واقعیت ثبت‌شده را از استنتاج جدا می‌کند. نام تمام شکست‌های آخرین اجرای کامل، بدون تبدیل آن‌ها به PASS، در `summary.json.remaining_full_suite_failures` و JUnit موجود است.

تفکیک دو اجرای کامل ضروری است: در `full-final-02`، حالت False این بار از assertion وضعیت `timed_out` عبور کرد ولی روی `history_complete=True` شکست خورد؛ بنابراین مشاهدهٔ «صفر ارزیابی و نبود identity» از اجرای اول به اجرای دوم تعمیم داده نمی‌شود. حالت True باز هم `timed_out` به‌جای `cancelled` داشت. آزمون identifier_tie نیز `provider_error` و تمام‌شدن صف پاسخ mock را ثبت کرد. علت ریشه‌ای این سه شکستِ اجرای دوم، صرفاً از JUnit به‌طور مستقل تأیید نمی‌شود. پیام واقعی هر assertion در `test_runs[].failure_messages` ثبت است؛ تشخیص‌های تفصیلی قدیمی صریحاً به اجرای اول منتسب‌اند.

آزمون هدفمند علاوه بر تمام فایل‌های `test_narrative*.py`، کنترل‌های واقعی Windows برای pilot، توقف worker/فرزند عددی و دو کنترل benchmark را داشت. آزمون‌های جدید رد ID، انتقال متن نامعتبر، اصلاح موفق، تشخیص مستقل sense، ثبات acceptedها، زنجیرهٔ writer/repair/reviewer مصنوعی، سقف تلاش/fallback، scope و hash نادرست، replay پس از وقفه، context قدیمی، درخواست wire و پاک‌سازی اطلاعات حساس را پوشش می‌دهند.

دستور ثبت‌شدهٔ اجرای کامل، از worktree نامزد:

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
& 'G:\\projects\\solverscout\\.venv\\Scripts\\python.exe' 'artifacts/narrative-contract-repair/20261113T105151Z-e151aec/run_offline.py' '{full['run_label']}' '--durations=15'
```

runner مسیر `src` همین worktree را در PYTHONPATH قرار می‌دهد و `python -m pytest -q -m "not live_provider" -p offline_network_guard --junitxml=...` را اجرا می‌کند. این کار مانع استفادهٔ اتفاقی از editable-install worktree اصلی می‌شود. دستور دقیق هر اجرا، زمان، exit، نسخهٔ Python و بسته‌ها در `offline/*-result.json`؛ خروجی کامل در `.log`؛ و نتیجهٔ هر testcase در `.xml` موجود است. اجرای کامل روی SHA commit‌شده و بدون تغییر کد ثبت‌نشده آغاز شد. عدد تاریخی ۷۷۰ در تصمیم قبولی دخالت ندارد.

اجرای هدفمند ۲۵۶ موردی پیش از commit پیاده‌سازی انجام شده بود؛ بنابراین HEAD آن در result برابر commit ادغام و پنج مسیر تغییرکرده نیز صریح ثبت شده‌اند. اجرای کامل نهایی مستقلاً روی SHA commit‌شده انجام شد و manifest بایت‌های منبع و harness پیش و پس از آن مقایسه شدند. evidence index این دو نوع نسبت به نسخه را یکسان معرفی نمی‌کند.

## نمونهٔ امن repair و اتصال نسخه

`synthetic-repair-example.json` خروجی واقعیِ مولد آفلاین `generate_synthetic_repair_example.py` است و صریحاً `SYNTHETIC_NOT_HISTORICAL` دارد. نمونه با `problem-main`، مسیر `sections[0].paragraphs[0].paragraph_id`، نوع `string_pattern_mismatch`، regex دقیق و راهنمای `problem_main` ثبت شده؛ متن و توکن‌های بخش در snapshot باقی می‌مانند. hash درخواست repair و چهار بخش سالم، قبولی پاسخ اصلاح‌شده و خطای مستقل نبود sense نیز ثبت شده‌اند. prompt hash نمونه عمداً مصنوعی است؛ معادل hash واقعی prompt نصب‌شده ادعا نمی‌شود. این مولد بازبین معنایی اجرا نمی‌کند؛ اثبات زنجیرهٔ بازبینیِ شبیه‌سازی‌شده در آزمون‌های واقعی است.

گزیدهٔ واقعیِ همان نمونهٔ مصنوعی برای اطلاعات دریافتی repair:

```json
{safe_error_example}
```

hash منبع طبق تعریف runtime_manifest در harness موجود:

`{source['canonical_source_sha256']}`

hash harness/آزمون‌ها طبق تعریف harness_manifest:

`{harness['canonical_harness_sha256']}`

این hashها از بایت فایل‌های worktree محاسبه شده‌اند و line ending ویندوز را دربرمی‌گیرند؛ با Git blob SHA یکسان فرض نمی‌شوند. فهرست کامل مسیر/hash در `source_manifest.json` و `harness_manifest.json` است. تفاوت کد از commit ادغام در `contract-repair.patch` ثبت است. `evidence_index.json` اندازه و SHA-256 هر فایل بسته را دارد. ZIP شامل همین شواهد منتخب و گزارش‌هاست؛ فایل‌های خصوصی، دیتابیس، .env، credential store، cookie و header واقعی اتصال وارد آن نشده‌اند.

## حدود نتیجه و ادامهٔ دقیق

- `new_model_calls = 0` و `new_real_model_tokens = 0`.
- `new_objective_evaluations_outside_required_tests = 0` و `new_solver_calls_outside_required_tests = 0`.
- تست‌های عددی موجود واقعاً ارزیابی/حل انجام می‌دهند؛ مجموع آن‌ها در این مأموریت شمارش سراسری نشده و صفر ادعا نمی‌شود.
- marker زنده انتخاب نشده و متغیر تنظیم provider زنده در subprocess آزمون حذف شده است. guard پردازهٔ pytest و threadهایش فقط loopback را مجاز می‌کند؛ گزارش آن ممیزی سراسری شبکهٔ همهٔ پردازه‌ها نیست.
- تمام سناریوهای زندهٔ نسخهٔ جدید، از C-A1/C-A2 تا C-L1 و C-E1-R، `NOT_RUN` هستند. mock reviewer به معنی آزمون زنده نیست.
- طبق گزارش تاریخیِ ثبت‌شده، C-L1 شکست خورده بود؛ اسناد تاریخی FAIL و handoff با blobهای commit ad449 برابر و دست‌نخورده‌اند. پاسخ خام تاریخی در بازیابی قبلی پیدا نشده بود و این مأموریت آن را بازسازی یا مستقلاً تأیید نکرده است. نبود artifact تاریخی شکست تازه محسوب نمی‌شود.

برای پذیرش زندهٔ آینده، فایل واقعی E1 از `artifacts/narrative-v3/20261112T050404Z-3914568/live/E1/report.json` یا کپی معتبر `artifacts/narrative-v3-closure/20261112T110238Z-fbecb6e/inputs/E1.json` باید از سیستم قبلی منتقل شود. برای بازبینی تاریخی C-L1 نیز پوشهٔ کامل کمپین قبلی `artifacts/narrative-v3-closure/20261112T110238Z-fbecb6e/` لازم است. فایل‌های مصنوعی جای آن‌ها نیستند.

دستورهای دقیق آماده‌سازی، بررسی واقعی UI/کنترل‌های adversarial، freeze و live/resume در `Next_Live_Acceptance_FA.md` آمده‌اند. پس از فراهم‌شدن واقعی پیش‌نیازهای آن سند، فرمان live آینده چنین است؛ در مأموریت حاضر اجرا نشده است:

```powershell
& $Python scripts/qa_narrative_v3_closure.py --mode live --fixture-root $Fixtures --archived-e1 $ArchivedE1 --evidence-root $Campaign --provider-data-dir $ProviderData --resume
```

کمپین باید تازه باشد و freeze به همین source/harness و auditهای واقعی متصل شود. موفقیت آزمون‌های هدفمند این مأموریت، به‌تنهایی freeze، کیفیت زندهٔ مدل یا آمادگی تحویل را تأیید نمی‌کند؛ اجرای کامل نهایی فعلاً FAIL است. پس از ثبت و ارسال همین شاخه، مأموریت حاضر متوقف می‌شود.
'''
(ROOT / 'Contract_Repair_Status_FA.md').write_text(report, encoding='utf-8')
print(json.dumps({'offline_status': summary['offline_repair_and_regression_status'],
                  'full_counts': full['junit_counts'], 'new_cases': summary['new_contract_regression_cases']}, indent=2))
