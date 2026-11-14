# راهنمای پذیرش زندهٔ بعدی SolverScout

این سند صرفاً دستور اجرای آینده است. فرمان‌های این سند در مأموریت اصلاح قرارداد اجرا نشده‌اند؛ پذیرش زندهٔ نسخهٔ جدید `NOT_RUN` است. بودجهٔ تماس واقعی مدل در مأموریت جاری صفر است و آغاز کمپین زنده نیازمند مجوز جداگانهٔ آینده است. ساخت این راهنما به معنی آماده‌بودن freeze یا تأیید تحویل نهایی نیست.

مبنای راهنما، خواندن `scripts/qa_narrative_v3_closure.py` و توابع `prepare`، `freeze_candidate`، `run_live` و `complete_human_review` است. CLI فقط حالت‌های `offline`، `live` و `verify` دارد؛ ساخت freeze باید با فراخوانی صریح تابع انجام شود.

پیش از اجرای آینده، این موارد باید واقعاً فراهم باشند:

- شاخهٔ نامزد با تمام تغییرات کد و آزمون‌ها commit شده و خروجی `git status --porcelain` خالی باشد. پس از آماده‌سازی و freeze، سورس، promptها، harness، fixtureها و profile ثابت بمانند.
- از پوشه و ledger تازه استفاده شود؛ ledger تاریخیِ `failed`، `blocked` یا `complete` با `--resume` به کمپین تازه تبدیل نمی‌شود. پوشهٔ پیشنهادی زیر خارج worktree است تا خروجی تولیدشده مانع شرط clean Git نشود.
- fixtureهای F1 تا F4 از `tests/fixtures/narrative_v3/` همین نامزد خوانده شوند.
- گزارش واقعی آرشیوی E1 از سیستم قبلی منتقل شود: `artifacts/narrative-v3/20261112T050404Z-3914568/live/E1/report.json` یا کپی همان گزارش در `artifacts/narrative-v3-closure/20261112T110238Z-fbecb6e/inputs/E1.json`. وجود آن در سیستم جدید در این راهنما ادعا نمی‌شود. فایل غایب با fixture مصنوعی یا حل تازه جایگزین نشود.
- کپی `report.redacted-projection-before-audit.json` برای E1 مناسب نیست؛ نسخهٔ معتبر باید رکورد عمومی `numeric_execution.authorization` را حفظ کند. harness برابری `core_hash(report)` با `narrative_v3.source_core_sha256` و حضور نامزدهای دارای `CMA_ES` و `.ABC:` را کنترل می‌کند.
- یک مسیر دادهٔ مشروعِ موجود برای provider مشخص شود؛ profile باید credential قابل‌دسترسی و `test_status == passed` داشته باشد. freeze و live اطلاعات ذخیره‌شده را می‌خوانند و کپی خصوصی QA می‌سازند. این راهنما فرمان connection test یا انتقال کلید ارائه نمی‌کند.
- محیط Python موجود با وابستگی‌های آزمون آماده باشد. محیط مشترک زیر ممکن است editable-install مربوط به worktree اصلی داشته باشد؛ تنظیم صریح `PYTHONPATH` الزامی است تا نامزد جدید import شود.

در PowerShell، دو مقدار داخل `<...>` را پیش از هر اجرا با مسیرهای واقعی جایگزین کنید. مسیر `$Campaign` را برای تمام مراحل و resumeهای همین کمپین حفظ کنید؛ در resume دوباره مقدار تازه تولید نکنید.

```powershell
Set-Location 'G:\projects\solverscout-narrative-contract-repair'
$Python = 'G:\projects\solverscout\.venv\Scripts\python.exe'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Remove-Item Env:PYTEST_ADDOPTS -ErrorAction SilentlyContinue
Remove-Item Env:PMC_TEST_LIVE_PROVIDER_CONFIG -ErrorAction SilentlyContinue
$env:PYTHONPATH = Join-Path $PWD 'src'
$Fixtures = Join-Path $PWD 'tests\fixtures\narrative_v3'
$ArchivedE1 = '<absolute path to restored valid E1 report.json>'
$ProviderData = '<absolute path to existing provider data directory>'
$CandidateSha = (git rev-parse HEAD).Trim()
$Campaign = "G:\projects\solverscout-live-evidence\$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))-$($CandidateSha.Substring(0,7))"

if (git status --porcelain) { throw 'Commit candidate changes before preparing the campaign' }
if (-not (Test-Path -LiteralPath $ArchivedE1 -PathType Leaf)) { throw 'Restore the real archived E1 report first' }
if (Test-Path -LiteralPath $Campaign) { throw 'Use a new campaign directory; preserve existing evidence' }

& $Python scripts/qa_narrative_v3_closure.py --mode offline --fixture-root $Fixtures --archived-e1 $ArchivedE1 --evidence-root $Campaign
if ($LASTEXITCODE -ne 0) { throw 'Offline preparation failed' }

& $Python -m pytest -q -m 'not live_provider' --junitxml (Join-Path $Campaign 'offline\full-final.xml')
if ($LASTEXITCODE -ne 0) { throw 'Offline regression failed' }
if ((git rev-parse HEAD).Trim() -ne $CandidateSha -or (git status --porcelain)) { throw 'Candidate changed during preparation or tests; start fresh evidence' }
```

حالت offline از MockTransport استفاده می‌کند؛ نتیجهٔ آن سنجش کیفیت زندهٔ مدل نیست. اجرای مجموعهٔ pytest ممکن است کار عددیِ آزمون‌های ضروری را انجام دهد؛ آن را با صفر ارزیابی تازهٔ کمپین گزارش‌فقط یکسان ندانید. خروجی، exit code و JUnit اجرای آینده باید حفظ شوند؛ تعداد تاریخی آزمون‌ها معیار قبولی نیست.

اکنون پیش از freeze توقف کنید و بررسی واقعی نمایش و کنترل‌های مصنوعی را انجام دهید. دو audit زیر خروجی خودکار CLI آفلاین نیستند و نباید با درج صوری `PASS` ساخته شوند. تمام پیش‌نیازهای تابع freeze چنین‌اند:

| مسیر نسبت به `$Campaign` | شاهد لازم |
|---|---|
| `offline/preparation.json` | وضعیت `PASS`، همراه runtime و harness برابر نسخهٔ جاری؛ توسط آماده‌سازی آفلاین تولید می‌شود. |
| `offline/full-final.xml` | JUnit اجرای کامل همین نامزد، حداقل یک testcase و بدون failure، error یا skipped. |
| `ui/offline-browser-summary.json` | نتیجهٔ واقعی بررسی نمایش با `status` و `source_manifest_sha256`؛ برای عبور، وضعیت `PASS` و hash منبع برابر `offline/preparation.json.runtime.canonical_source_sha256` باشد. |
| `offline/adversarial-input-audit.json` | نتیجهٔ واقعی بازخوانی کنترل‌های مثبت و ادعاهای عمدی در `prepared/C-A1.json` و `prepared/C-A2.json`؛ برای عبور، `status` برابر `PASS` و نگاشت `prepared_cases` دقیقاً برابر همان نگاشت در preparation باشد. دلایل و شواهد بررسی نیز ثبت شوند. |

بستهٔ اصلاح قراردادِ فعلی صرفاً با داشتن تست و JUnit، این چهار شرط freeze را خودبه‌خود تأمین نمی‌کند. JUnit قدیمی یا audit تاریخی جای بررسی همان نامزد و همان prepared cases را نمی‌گیرد. تولید آفلاین پس از freeze بسته است؛ در صورت تغییر نسخه، شواهد قبلی حفظ و نامزد/کمپین جدید با مجوز مناسب آماده شود.

پس از تکمیل واقعی پیش‌نیازها، ثبت تمام تغییرات نامزد و کنترل clean Git، freeze را بدون چاپ اطلاعات provider ایجاد کنید:

```powershell
if ((git rev-parse HEAD).Trim() -ne $CandidateSha -or (git status --porcelain)) { throw 'Candidate changed; repeat preparation, full regression and audits in fresh evidence' }
& $Python -c "import sys; from pathlib import Path; sys.path.insert(0, str(Path.cwd() / 'scripts')); from qa_narrative_v3_closure import freeze_candidate; freeze_candidate(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())" $Campaign $ProviderData
if ($LASTEXITCODE -ne 0) { throw 'Candidate freeze failed; inspect the unmet prerequisite' }
```

این مرحله `freeze.json`، `source_manifest.json` و `harness_manifest.json` را ایجاد و manifest کمپین را تکمیل می‌کند. تابع فقط یک freeze می‌پذیرد. پرونده‌های خصوصی `_private_qa`، credential، cookie و header اتصال وارد بستهٔ قابل‌انتقال نشوند.

فرمان دقیق آغاز زنده، فقط پس از مجوز جداگانه و freeze موفق:

```powershell
if ((git rev-parse HEAD).Trim() -ne $CandidateSha -or (git status --porcelain)) { throw 'Candidate changed after freeze; do not resume this campaign' }
& $Python scripts/qa_narrative_v3_closure.py --mode live --fixture-root $Fixtures --archived-e1 $ArchivedE1 --evidence-root $Campaign --provider-data-dir $ProviderData --resume
if ($LASTEXITCODE -ne 0) { throw 'Campaign failed or is blocked; preserve its ledger and evidence' }
```

سقف موجود harness برابر ۲۲ تلاش نقش است: برای هر کنترل بازبین حداکثر یک تماس، و برای هر گزارش حداکثر چهار تماس با سقف دو نویسنده و دو بازبین. این سقف مجوز مصرف در مأموریت جاری نیست. ترتیب سناریوها `C-A2 → C-A1 → C-L1 → C-L4 → C-L2 → C-L3 → C-E1-R` است؛ هیچ حل‌گر یا ارزیابی تابع تازه در این کمپین گزارش‌فقط مجاز نیست.

خروجی موفق فرمان می‌تواند `awaiting_cause_review` یا `awaiting_quality_review` باشد؛ exit صفر به‌تنهایی PASS کمپین نیست. برای ادامه، پاسخ/گزارش واقعیِ همان مرحله خوانده و audit مربوط ثبت شود:

- کنترل‌های C-A1/C-A2: `live/<scenario>/adversarial_audit.json` شامل `response_sha256` برابر SHA-256 بایت‌های `response.json`، `context_hash` برابر summary، وضعیت واقعی `PASS` یا `FAIL` و `reason_fa` مستند دربارهٔ ارتباط علت رد با ادعای عمدی و پذیرش کنترل مثبت.
- گزارش‌ها: `live/<scenario>/quality_review.json` شامل `report_sha256` برابر summary و شش ردیف `rubric` با شناسه‌های Q1 تا Q6؛ هر ردیف وضعیت واقعی، `reason_fa` و `paragraph_ids` موجود در متن rendered همان گزارش داشته باشد. خروجی fallback یا mock، متن پذیرفته‌شدهٔ مدل محسوب نشود. معیارهای کیفیت باید واقعاً بررسی شوند؛ تطبیق ساختاری JSON به‌تنهایی ارزیابی کیفیت نیست.

سپس **همان فرمان live بالا، با همان `$Campaign` و `--resume`** اجرا شود. در نخستین FAIL قطعی یا BLOCKED، کمپین بسته می‌شود؛ ledger یا نتیجه برای ادامهٔ اجباری بازنویسی نشود. سناریوهای نرسیده `NOT_RUN` می‌مانند.

ممیزی خواندنیِ شواهد همان کمپین با فرمان زیر ممکن است؛ قبولی ممیزی جای قبولی همهٔ سناریوها و بررسی آمادگی تحویل را نمی‌گیرد:

```powershell
& $Python scripts/qa_narrative_v3_closure.py --mode verify --fixture-root $Fixtures --archived-e1 $ArchivedE1 --evidence-root $Campaign
```

در تهیهٔ این راهنما، دو سند تاریخی `docs/FINAL_NARRATIVE_CLOSURE_2026_11_12_FA.md` و `docs/Delivery_Preparation_Handoff_FA.md` با commit `ad449d252f243ff0400e3d42caae5d25defe5b11` مقایسه شدند و تفاوتی نداشتند. گزارش تاریخی FAIL حفظ شده و با این دستور آینده بازنویسی نشده است.
