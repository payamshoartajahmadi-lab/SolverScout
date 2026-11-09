# ماتریس پیاده‌سازی و پذیرش P_MC نسخهٔ ۱

| گام | خروجی و شاهد اجرایی | وضعیت دروازه |
| --- | --- | --- |
| P0 | بررسی Git، Python 3.12.16، نصب runtime و test lock و `pip check` | PASS |
| P1 | مدل‌های typed، DSL مفسری، hash، نمونه‌های تحلیلی و schemaهای تولیدشده | PASS |
| P2 | ارزیاب، ثبت سهم، فرایند spawn، آزمون crash، timeout و علامت | PASS |
| P3 | چهار Adapter دقیق MEALPY، پایلوت و رتبه‌بندی | PASS |
| P4 | SQLite، API محلی، Worker و آزمون مرگ، لغو و ارسال تکراری | PASS |
| P5 | تنظیمات، مخزن کلید، prompt و آزمون‌های transport و workflow | PASS برای fixture |
| P5 زنده | اتصال واقعی و نقش‌های واقعی مدل | BLOCKED |
| P6 | LangGraph و خروجی عددی و مسیرهای خطای fixture | PASS برای مسیرهای یکپارچهٔ آزموده‌شده و رابط Chromium |
| P7 | ۱۶ اجرای واقعی دودزا و manifest بزرگ بدون اجرا | PASS |
| P8 | راهنما، ZIP، نصب تازه و بازرسی | PASS در macOS arm64 |

## ردیابی سناریوهای سند

وضعیت PASS فقط به موارد اجراشده در محیط و fixture مشخص ارجاع دارد. در ردیف‌های ترکیبی، دامنهٔ پوشش کنار شناسه‌ها آمده است.

| شناسهٔ سند | شاهد و وضعیت |
| --- | --- |
| DAT-001 تا DAT-012، DAT-019 و DAT-020 | PASS، `tests/test_numeric.py` |
| DAT-013 تا DAT-015 | PASS برای منشأ profile در fixture شکست main و نگاشت اعتبار و بهینگی در گزارش. موارد رد و تأیید در تست‌ها به‌صورت جدا ثبت شده‌اند |
| DAT-016 | PASS، صورت‌بندی و رفع ابهام با fixture فارسی. کیفیت مدل واقعی BLOCKED |
| DAT-017 | کنترل اتصال Trial به snapshot پیاده شده است. تزریق عمدی revision نامرتبط در این مسیر NOT_TESTED |
| DAT-018 | PASS برای اسکن sentinel مصنوعی و مسیرهای export/DB در gateway. کلید واقعی BLOCKED |
| API-001 تا API-016 | PASS در ترکیب persistence، gateway و workflow با fixture. مسیر خطای provider با تزریق transport آزموده شد |
| LLM-001 تا LLM-018 | PASS برای قراردادهای DTO، gateway و workflow با TEST FIXTURE. قابلیت حساب واقعی و کیفیت خروجی BLOCKED |
| EXE-001 تا EXE-017 و EXE-019/020 | PASS در suite عددی و persistence. لغو/بازیابی فعال در loopback هم اجرا شد |
| EXE-018، SEL-005 تا SEL-007 | PASS برای گزینش ناکافی و عدم شروع main پس از شکست پایلوت‌ها، و حفظ نقطهٔ profile هنگام شکست main در اجرای خط پایه. حالت دقیق پایلوت موفق در فهرست NOT_TESTED آمده است |
| SEL-001 تا SEL-004 و SEL-008/009 | PASS، fixture رتبه‌بندی و چهار Adapter واقعی |
| SEL-010 تا SEL-012 | PASS برای تفکیک fixture، baseline واقعی و حذف دادهٔ مرجع از context و درخواست solver |
| LOC-03 و LOC-16 | BLOCKED، اطلاعات اتصال واقعی در اختیار توسعه قرار نگرفت |
| LOC-04 تا LOC-10 و LOC-12 | PASS برای fixtureهای gateway و آزمون خطا. گزینهٔ اختیاری فهرست مدل‌ها به ورود دستی واگذار شده است |
| LOC-01/02 و LOC-13 تا LOC-15 | PASS در Chromium و نصب استخراج‌شدهٔ macOS arm64 |
| LOC-11 و SYS-016 | PASS برای مسیر بدون تماس provider. قطع فیزیکی شبکه آزموده نشده است |
| PKG-01 تا PKG-04، SYS-015 | PASS برای بازرسی ZIP، نصب تازه و حفظ داده در macOS arm64. آزمون rollback مهاجرت PASS. سکوی دیگر NOT_TESTED |
| PKG-05 و PKG-06 | PASS برای تفکیک آزمون زنده و نمایش مصرف و هزینهٔ نامعلوم |
| SYS-001 تا SYS-014 و SYS-017 تا SYS-021 | PASS برای قراردادهای نمونه، اجرای عددی، چرخهٔ لغو و replay، خروجی امن و آزمون‌های gateway. سازگاری schema با API واقعی BLOCKED |

مجموعهٔ استاندارد آزمون‌ها با `.venv/bin/python -m pytest -q` اجرا می‌شود. نام و نتیجهٔ suiteها در [گزارش آزمون](TEST_RESULTS.md) ثبت شده است.


## سناریوهای دقیقِ باقی‌مانده

| شناسه | دامنهٔ آزمون‌نشده | وضعیت |
| --- | --- | --- |
| DAT-016 | همهٔ ترکیب‌های ابهام معنایی جهت و قید در متن فارسی. نمونهٔ کران ناقص با fixture آزموده شد | NOT_TESTED |
| DAT-019 | بازنمایی timeout بازبینی در مرورگر. نتیجهٔ عددی timeout و عدم اعتبارسنجی در تست موتور آزموده شد | NOT_TESTED |
| API-004 | ادامهٔ کامل با جواب متنی در یک تماس دوم. mutation متنی و ادامهٔ ساخت‌یافته جداگانه آزموده شدند | NOT_TESTED |
| API-006 | مشاهدهٔ تکرار و dedup cursor در مرورگر. توالی و cursor از API آزموده شدند | NOT_TESTED |
| API-011 | قطع شبکه در هر دو نقش و ادامهٔ مسیر E2E. نگاشت خطای transport و توقف احراز هویت مشاور جدا آزموده شدند | NOT_TESTED |
| EXE-011 | ارسال دوبارهٔ همان execution key برای Trial فیزیکی. idempotency Job و StageExecution آزموده شدند | NOT_TESTED |
| EXE-016/017 | تکرار کامل گردش با تغییر `PYTHONHASHSEED` در فرایندهای مستقل. بازتولید preflight و مشتق‌سازی seed آزموده شد | NOT_TESTED |
| EXE-018، SEL-006/007 | شکست main پس از پایلوت موفق، بهتر بودن profile از پایلوت‌های موفق و ترکیب روش واجد/فاقد شرایط در آزمون سرتاسری. fixtureهای حفظ archive، شکست همهٔ پایلوت‌ها و معیار eligibility جدا آزموده شدند | NOT_TESTED |
| LOC-11 | قطع فیزیکی شبکه. اجرای عددی بدون credential، بدون تماس provider و با گزارش قالبی آزموده شد | NOT_TESTED |
| LOC-03/16 | حساب، مدل و endpoint زنده، شامل پذیرش schema نقش‌ها | BLOCKED |
