# گزارش اصلاح قرارداد و repair گزارش‌نویسی SolverScout

نتیجهٔ اصلاح و آزمون آفلاین: **FAIL**. پذیرش زندهٔ نسخهٔ جدید: **NOT_RUN**. آمادگی تحویل نهایی: **NOT_VERIFIED — هنوز تأیید نشده**.

نسخهٔ آزموده‌شده `d32691dfbb7e5f9faeafe5dcb119d65e97d46de3` در شاخهٔ `fix/narrative-contract-repair` است. اجرای کامل آفلاین 798 مورد موفق، 3 شکست، 0 خطای اجرا، 0 skipped و 1 deselected داشت؛ exit code برابر 1 بود. 31 مورد از 31 آزمون جدید repair نیز موفق بود. این نتیجه پذیرش زنده یا تأیید کیفیت مدل نیست.

این گزارش پیش از commit حامل شواهد تهیه شده است. آن commit فقط گزارش و شواهد را اضافه می‌کند؛ کد آزموده‌شده تغییر نمی‌کند. وضعیت واقعی push و SHA نهایی remote جداگانه پس از ارسال در پاسخ پایان اعلام می‌شود.

## Git و نسخهٔ مبنا

| مرجع | SHA کامل |
|---|---|
| dev محلی و HEAD اولیهٔ worktree اصلی | `e151aecc0a79a7e6b421b07934df6977bc9068c7` |
| origin/dev پس از fetch موفق | `fbecb6edae4b951aaaa1d364d5428500f4596786` |
| origin/fix/narrative-v3-closure پس از fetch | `ad449d252f243ff0400e3d42caae5d25defe5b11` |
| main محلی | `6837ba01451acb1d4c3cab898c8660daf6f063f0` |
| commit ادغام در نامزد جدید | `7ee1a98a2ca1159d7b75adf001215f644cea472a` |
| commit پیاده‌سازی repair و آزمون‌های جدید | `0a9908ea9f23081f6be559596765aa16911b8580` |
| commit کد و آزمون جدید؛ مبنای اجرای کامل | `d32691dfbb7e5f9faeafe5dcb119d65e97d46de3` |

dev در آغاز نسبت به origin/dev سه commit جلو و صفر commit عقب بود؛ تغییر تازه‌ای در origin/dev برای ادغام وجود نداشت. تغییر ثبت‌نشدهٔ tracked در آغاز نبود. worktree جداگانه در `G:\projects\solverscout-narrative-contract-repair` ساخته شد و closure بدون تعارض در همین شاخه ادغام شد. dev و main مقصد ادغام یا push این مأموریت نیستند و HEAD آن‌ها حفظ شده است.

مشاهدهٔ مستقیم remote پیش از انتشار در `2026-11-13T12:13:14.191442+00:00` با `git ls-remote` در `remote-before-publication.json` ثبت شده است؛ این مدرک مستقل از remote-tracking محلی است. نتیجهٔ push شاخهٔ جدید پس از ساخت commit شواهد بررسی می‌شود.

اصلاحات Windows در commitهای `5ddee26ed86cefe9585e3f89fe02fc7a9f4f7816` و `13184acb677a2dbe96524380057251027e73db41` و اصلاحات/اسناد closure در `b4723bf56d4ea76a0aec16f994ae87fa0e4b2713` و `ad449d252f243ff0400e3d42caae5d25defe5b11` همگی ancestor نسخهٔ آزموده‌شده هستند؛ مدرک در `git-provenance.json` است. هفت فایل پشتیبان و چهار فایل بستهٔ بازیابی قبلی حفظ شده‌اند. `preservation-check.json` hash فعلی، تطبیق سه کپی تست با رکورد preservation و تطبیق ZIP بازیابی با hash ثبت‌شده را دارد.

## علت تأییدشده و اصلاح انجام‌شده

در کد قبل از patch جدید، `provider_schema` عمداً محدودیت `pattern` را از schema ارسالی حذف می‌کرد. اعتبارسنج داخلی همچنان شناسهٔ دارای خط تیره را رد می‌کرد، اما خطای section به SCHEMA_INVALID عمومی تقلیل می‌یافت. orchestrator برای فرستادن همان section به repair دوباره `NarrativeSectionV3` را می‌ساخت و با همان خطا آن را کنار می‌گذاشت؛ بنابراین دادهٔ لازم از زمینهٔ repair حذف می‌شد.

این مسیر خرابی با خواندن کد و آزمون مصنوعی تأیید شده است. تطبیق مستقل آن با پاسخ خام تاریخی C-L1، به‌دلیل نبود پاسخ خام روی این سیستم، `NOT_VERIFIED` باقی می‌ماند.

در commit جدید:

- prompt نسخهٔ ۳٫۱، regex دقیق `^[a-z][a-z0-9_]{0,47}$`، نمونهٔ معتبر `problem_main`، نامعتبر بودن `problem-main` و یکتایی نسبت به بخش‌های پذیرفته‌شده را برای draft و repair صریح بیان می‌کند. شناسه به‌صورت خاموش تغییر داده نمی‌شود.
- قرارداد بسته و قابل‌اعتبارسنجی `InvalidSectionRepairV1` به فهرست موجود `failed_sections` افزوده شده است: حداکثر سه پاراگراف، دوازده خطای ساختاری و ۱۶۳۸۴ بایت برای هر snapshot. متن/ارجاع‌های لازم با سقف طول حفظ می‌شوند؛ تغییر یا حذف از snapshot با `truncated` مشخص است و raw اصلی تغییر نمی‌کند.
- خطا دارای section، مسیر دقیق فیلد، نوع خطا، قاعده و راهنمای فارسی است. Pydantic input/msg/ctx و فیلدهای اضافی منتقل نمی‌شوند. اطلاعات credential، header و مسیر خصوصی از رشته‌های snapshot حذف می‌شوند؛ نام عمومی `numeric_execution.authorization` کورکورانه حذف نمی‌شود.
- orchestrator همان snapshot را به repair می‌رساند و آن را متن معتبر یا پذیرفته‌شده نمی‌شمارد. request hash شامل snapshot است؛ شکل ریشهٔ context و قرارداد خروجی writer/reviewer ثابت مانده‌اند و contextهای قدیمی با hash قبلی roundtrip می‌شوند.
- الزام فیزیکی توکن `{{fact:problem.sense}}` و برابری توکن‌های متن با `fact_refs` حفظ شده است. راهنمای MISSING_REQUIRED_FACT اکنون خودِ توکن‌های غایب را نام می‌برد. اصلاح شناسه، خطای binding را رفع‌شده فرض نمی‌کند.

`models.py` و `provider.py` تغییر نکرده‌اند؛ آزمون‌ها بدنهٔ واقعی ساخته‌شده توسط `build_request` و `prepare_provider_request` را در هر سه حالت پاسخ بررسی می‌کنند. فرض پشتیبانی provider از pattern اضافه نشده است. patch جدید منطق انتخاب روش، حل‌گرها، seedها، بودجه‌ها، آستانه‌های عددی یا معیارهای پذیرش قبلی را تغییر نمی‌دهد. نسخهٔ evidence policy افزایش نیافته؛ hash prompt جدید طبیعتاً تغییر می‌کند و گزارش‌های ذخیره‌شدهٔ قدیمی همچنان خوانده می‌شوند.

## آزمون‌های واقعی این مأموریت

| اجرا | کل | passed | failed | errors | skipped | deselected | exit |
|---|---:|---:|---:|---:|---:|---:|---:|
| targeted-new-01 | 26 | 24 | 2 | 0 | 0 | 0 | 1 |
| targeted-final-01 | 256 | 241 | 15 | 0 | 0 | 0 | 1 |
| targeted-final-02 | 256 | 256 | 0 | 0 | 0 | 0 | 0 |
| full-final-01 | 801 | 797 | 4 | 0 | 0 | 1 | 1 |
| compatibility-final-01 | 32 | 32 | 0 | 0 | 0 | 0 | 0 |
| full-final-02 | 801 | 798 | 3 | 0 | 0 | 1 | 1 |

شکست‌های ثبت‌شده در آخرین اجرای کامل:

- `tests.test_llm_workflow::test_explainer_receives_full_tie_or_insufficient_evidence[identifier_tie]`
- `tests.test_numeric::test_exe_009_010_supervisor_reaps_hung_objective[False]`
- `tests.test_numeric::test_exe_009_010_supervisor_reaps_hung_objective[True]`

اجرای اول دو شکست واقعی در راهنمای `problem.sense` نشان داد؛ راهنما اصلاح شد و assertionها ضعیف نشدند. اجرای گستردهٔ اول ۱۴ شکست import مربوط به نبود `protego` و یک timeout راه‌اندازی fixture محلی API داشت. بررسی وابستگی‌های ثبت‌شده نشان داد `protego==0.7.0` و `pypdf==7.0.0` نصب نبودند. فقط همین دو وابستگی با `--no-deps` به محیط موجود اضافه شدند؛ نسخهٔ وابستگی‌های دیگر، specification پروژه یا معیارهای تست تغییر نکرد. اجرای بعدیِ همان کد تمام ۲۵۶ آزمون را گذراند. این شکست‌ها و لاگ‌هایشان حفظ شده‌اند.

در نخستین اجرای کامل، آزمون byte-exact مربوط به trial schemaها نیز بررسی شد. `schema-eol-diagnosis.json` نشان می‌دهد فایل v1 دقیقاً ۱۳۲ و فایل v2 دقیقاً ۱۷۹ بایت CR ناشی از checkout ویندوز داشتند؛ تبدیل CRLF به LF در حافظه هر دو را دقیقاً برابر خروجی فعلی مدل کرد. وضعیت ثبت اصلاح پایان‌خط در `.gitattributes`: `True`. این اصلاح محتوا یا assertion را تغییر نمی‌دهد و schemaها دوباره تولید نشده‌اند. هر اجرای کامل و نام testcaseهای شکست‌خوردهٔ آن در summary و JUnit همان اجرا محفوظ است.

در مقایسهٔ تکمیلی، یک اختلاف قدیمی نامرتبط در `execution_result.v1.json` دیده شد: مدل از commit تاریخی `964651875a96b7808c268a5d519df6b56a81017f` فیلد اختیاری `runtime_provenance` دارد، ولی JSON ثبت‌شده آن را ندارد. این مورد خارج دامنهٔ repair باقی ماند و ادعای رفع آن نشده است؛ موفقیت pytest به معنی تطبیق همهٔ schemaهای static نیست.

سه شکست دیگر نخستین اجرای کامل عمداً با تغییر بودجه یا معیار مخفی نشدند. در `test_explainer_receives_full_tie_or_insufficient_evidence[identifier_tie]`، رکورد واقعیِ آزمون مصنوعی نشان داد بودجهٔ ۴۵ ثانیه‌ای پس از شش pilot کامل رو به اتمام بود؛ نخستین pilot مربوط به PSO فقط ۰٫۵۱۷ ثانیه مهلت داشت و با صفر ارزیابی timeout شد. انتخاب `not_reached` و decision تهی بود؛ mock با این وجود ادعای شکستن تساوی داشت. اعتبارسنج درست خطای `selection_explanation_fa: no selection was made` داد و repair بعدی صف mock را تمام کرد. پروژکشن امن این رکوردها در `offline/legacy-identifier-tie-diagnostic.json` است؛ هیچ دیتابیس یا credential صادر نشده است.

در دو مورد `test_exe_009_010_supervisor_reaps_hung_objective`، fixture مهلت کل ۱٫۸ ثانیه دارد و deadline پیش از spawn شروع می‌شود. برای حالت False، JUnit صفر ارزیابی و نبود runtime identity را نشان می‌دهد؛ برای True، فقط `timed_out` به‌جای `cancelled` در شکست ثبت شده است. این مشاهدات با تمام‌شدن مهلت پیش از رسیدن به تابع معلق سازگارند؛ عامل دقیق کندی و انتساب آن به patch گزارش‌نویسی `NOT_VERIFIED` است. موفقیت reaping یا cancellation از این نتایج استنتاج نمی‌شود. کد عددی و assertionهای این آزمون‌ها تغییر نکرده‌اند؛ `numeric-startup-diagnosis.json` واقعیت ثبت‌شده را از استنتاج جدا می‌کند. نام تمام شکست‌های آخرین اجرای کامل، بدون تبدیل آن‌ها به PASS، در `summary.json.remaining_full_suite_failures` و JUnit موجود است.

تفکیک دو اجرای کامل ضروری است: در `full-final-02`، حالت False این بار از assertion وضعیت `timed_out` عبور کرد ولی روی `history_complete=True` شکست خورد؛ بنابراین مشاهدهٔ «صفر ارزیابی و نبود identity» از اجرای اول به اجرای دوم تعمیم داده نمی‌شود. حالت True باز هم `timed_out` به‌جای `cancelled` داشت. آزمون identifier_tie نیز `provider_error` و تمام‌شدن صف پاسخ mock را ثبت کرد. علت ریشه‌ای این سه شکستِ اجرای دوم، صرفاً از JUnit به‌طور مستقل تأیید نمی‌شود. پیام واقعی هر assertion در `test_runs[].failure_messages` ثبت است؛ تشخیص‌های تفصیلی قدیمی صریحاً به اجرای اول منتسب‌اند.

آزمون هدفمند علاوه بر تمام فایل‌های `test_narrative*.py`، کنترل‌های واقعی Windows برای pilot، توقف worker/فرزند عددی و دو کنترل benchmark را داشت. آزمون‌های جدید رد ID، انتقال متن نامعتبر، اصلاح موفق، تشخیص مستقل sense، ثبات acceptedها، زنجیرهٔ writer/repair/reviewer مصنوعی، سقف تلاش/fallback، scope و hash نادرست، replay پس از وقفه، context قدیمی، درخواست wire و پاک‌سازی اطلاعات حساس را پوشش می‌دهند.

دستور ثبت‌شدهٔ اجرای کامل، از worktree نامزد:

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
& 'G:\projects\solverscout\.venv\Scripts\python.exe' 'artifacts/narrative-contract-repair/20261113T105151Z-e151aec/run_offline.py' 'full-final-02' '--durations=15'
```

runner مسیر `src` همین worktree را در PYTHONPATH قرار می‌دهد و `python -m pytest -q -m "not live_provider" -p offline_network_guard --junitxml=...` را اجرا می‌کند. این کار مانع استفادهٔ اتفاقی از editable-install worktree اصلی می‌شود. دستور دقیق هر اجرا، زمان، exit، نسخهٔ Python و بسته‌ها در `offline/*-result.json`؛ خروجی کامل در `.log`؛ و نتیجهٔ هر testcase در `.xml` موجود است. اجرای کامل روی SHA commit‌شده و بدون تغییر کد ثبت‌نشده آغاز شد. عدد تاریخی ۷۷۰ در تصمیم قبولی دخالت ندارد.

اجرای هدفمند ۲۵۶ موردی پیش از commit پیاده‌سازی انجام شده بود؛ بنابراین HEAD آن در result برابر commit ادغام و پنج مسیر تغییرکرده نیز صریح ثبت شده‌اند. اجرای کامل نهایی مستقلاً روی SHA commit‌شده انجام شد و manifest بایت‌های منبع و harness پیش و پس از آن مقایسه شدند. evidence index این دو نوع نسبت به نسخه را یکسان معرفی نمی‌کند.

## نمونهٔ امن repair و اتصال نسخه

`synthetic-repair-example.json` خروجی واقعیِ مولد آفلاین `generate_synthetic_repair_example.py` است و صریحاً `SYNTHETIC_NOT_HISTORICAL` دارد. نمونه با `problem-main`، مسیر `sections[0].paragraphs[0].paragraph_id`، نوع `string_pattern_mismatch`، regex دقیق و راهنمای `problem_main` ثبت شده؛ متن و توکن‌های بخش در snapshot باقی می‌مانند. hash درخواست repair و چهار بخش سالم، قبولی پاسخ اصلاح‌شده و خطای مستقل نبود sense نیز ثبت شده‌اند. prompt hash نمونه عمداً مصنوعی است؛ معادل hash واقعی prompt نصب‌شده ادعا نمی‌شود. این مولد بازبین معنایی اجرا نمی‌کند؛ اثبات زنجیرهٔ بازبینیِ شبیه‌سازی‌شده در آزمون‌های واقعی است.

گزیدهٔ واقعیِ همان نمونهٔ مصنوعی برای اطلاعات دریافتی repair:

```json
{
  "evidence_kind": "SYNTHETIC_NOT_HISTORICAL",
  "section_id": "problem",
  "paragraph_id": "problem-main",
  "structural_error": {
    "field_path": "sections[0].paragraphs[0].paragraph_id",
    "error_type": "string_pattern_mismatch",
    "expected_rule": "^[a-z][a-z0-9_]{0,47}$; unique across all new and accepted paragraphs",
    "repair_instruction_fa": "paragraph_id را خودتان با الگوی ^[a-z][a-z0-9_]{0,47}$ بسازید: ابتدا حرف کوچک لاتین، سپس فقط حروف کوچک، رقم یا زیرخط؛ خط تیره مجاز نیست. نمونهٔ معتبر problem_main است. شناسه در کل پاراگراف‌های جدید و پذیرفته‌شده یکتا بماند؛ سرور شناسه را خودکار تغییر نمی‌دهد."
  }
}
```

hash منبع طبق تعریف runtime_manifest در harness موجود:

`bf35bb3e06f96032c9d062b6a88f808d42405c7af939b1d9ed9a70d3b553d85c`

hash harness/آزمون‌ها طبق تعریف harness_manifest:

`747d2cda2cc68b6f52b81de6da549c8f414f9886768db2e12d033fa4e1547ae3`

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
