# منشأ مدارک نخستین تحویل دانشگاهی ویندوز

این پوشه گزیدهٔ قابل انتشار از شواهد موجود است. در مرحلهٔ ثبت انتشار، آزمون، تماس مدل، تست اتصال، آماده‌سازی یا اجرای حل‌گر تازه انجام نشده است. فایل ZIP آزموده‌شده نیز بازسازی نشده است.

| مرجع | شناسهٔ دقیق |
| --- | --- |
| کد آزموده‌شده و گیت نهایی آفلاین | `7ca14742dc34933b1c4868f113412aacc59f1f70` |
| commit منبع در MANIFEST بسته | `32e222e21550f0b881c7e54f180043c7589c13bc` |
| راهنمای منتشرشدهٔ نصب ویندوز | `0bc36db6cf04d3976a91bba54b025cf0428c249d` |
| commit محلی شواهد بازتولید گزارش U1 | `7ea72c8919fb3230e79f103457ed6dd23d9d8938` |
| commit محلی شواهد نصب تمیز ویندوز | `088ee6cf35597114c30114ba5929b12e5e8520dc` |

دو commit محلی آخر و فایل‌های اصلی آن‌ها حفظ شده‌اند؛ این دو commit به‌طور کامل منتشر نمی‌شوند، چون بخشی از مدارک عملیاتی آن‌ها مسیرهای شخصی و ابزارهای موقت بررسی را دارد. گزیدهٔ عمومی این پوشه از همان فایل‌ها استخراج شده و رابطهٔ هر فایل با commit و مسیر مبدأ، SHA-256 اصلی و SHA-256 عمومی در [manifest منشأ](evidence/provenance.json) ثبت است. حذف نسخهٔ اصلی یا بازنویسی تاریخچه انجام نشده است.

در شش فایل فراداده، فقط پیشوند مسیر خانهٔ کاربر یا checkout توسعه با `<WINDOWS_USER_HOME>` و `<DEVELOPMENT_CHECKOUT>` جایگزین شده است. مقادیر اصلی شخصی در نگاشت عمومی درج نشده‌اند. سایر نسخه‌های انتخاب‌شده، جز خلاصهٔ ممیزی بسته، با فایل اصلی یکسان‌اند. خلاصهٔ ممیزی بسته جدول حجیم اعضای ZIP را حذف می‌کند و نتایج بررسی و hash فایل ممیزی کامل را نگه می‌دارد. گزارش اصلی U1، HTML و journal، شامل متن پاسخ‌ها، اعداد و ارجاع‌های شواهد، **بایت‌به‌بایت با اصل یکسان‌اند**. این گزینش یا حذف مسیرها، پاسخ تاریخی مدل را بازنویسی نمی‌کند.

مدارک اصلی قابل بررسی:

- [ماتریس پانزده معیار پس از بازتولید گزارش](evidence/report-replay/acceptance_matrix.csv) و [خلاصهٔ پذیرش همان مرحله](evidence/report-replay/acceptance_summary.json): چهارده PASS، معیار AC14 مک تکمیل‌نشده و نتیجهٔ جامع `NOT_READY`.
- [گزارش ذخیره‌شدهٔ U1](evidence/report-replay/live/U1-report/report.json)، [نسخهٔ HTML](evidence/report-replay/live/U1-report/report.html)، [journal واقعی](evidence/report-replay/live/U1-report/journal.json) و [ممیزی کیفیت](evidence/report-replay/live/U1-report/human-quality-audit.json): پنج بخش مدل با اعتبارسنجی و بازبینی معنایی پذیرفته شدند. این موفقیت، پذیرش گزارش بازتولیدشده بر پایهٔ محاسبات ذخیره‌شده است؛ اجرای کامل تازهٔ U1 یا U2 نیست.
- [بررسی UI و HTML گزارش](evidence/report-replay/browser-qa/final-summary.json): نمایش در عرض‌های ۱۴۴۰ و ۳۹۰ پیکسل بررسی شده است. وضعیت موقت `PENDING` در خروجی اولیهٔ runner تاریخی است؛ نتیجهٔ نهایی UI در همین مدرک جداگانه ثبت شده و خروجی اولیه بازنویسی نشده است.
- [گیت آفلاین نهایی](evidence/report-replay/offline/summary.json): ۱۰۲۳ آزمون موفق روی کد 7ca؛ [تلاش قبلی](evidence/report-replay/offline/attempt-01/summary.json) نیز محفوظ است. این نتایج از رکورد تاریخی ۹۶۵ آزمون موفق مک جدا هستند.
- [نصب تمیز ویندوز](evidence/clean-install/summary.json)، [ممیزی همان ZIP](evidence/clean-install/package-audit-summary.json)، [محیط مستقل نصب](evidence/clean-install/installed-environment.json)، [توقف](evidence/clean-install/runtime-stopped.json) و [راه‌اندازی مجدد](evidence/clean-install/runtime-restarted.json): PASS با venv و data-dir مستقل. این مرحله پس از بازتولید گزارش انجام شده است؛ عبارت `NOT_RUN` برای نصب تمیز در خلاصهٔ مرحلهٔ قبلی، وضعیت تاریخی همان زمان است.
- [مصرف تاریخی rc2 ویندوز](evidence/historical-windows-rc2/consumption_summary.json)، [ledger گزارش rc3](evidence/report-replay/live/U1-report/campaign_ledger.json) و [ledger نصب](evidence/clean-install/usage-ledger.json): ۱۷ ارسال تاریخی، چهار ارسال جداگانهٔ بازتولید گزارش با صفر ارزیابی تازه، و یک smoke نصب با ۱۰۰۲ ارزیابی. این اعداد به مرحلهٔ انتشار افزوده یا از سقف‌های قبلی کسر نشده‌اند.

نام بسته `p_mc_optimizer-2.1.0rc3.zip` و SHA-256 آن ثابت است:

```text
94e88d45658676a1f9f9e62474d4db3bbe5c1f998c6085f4f266da6dced9e760
```

تگ انتشار، همان کد اجرایی آزموده‌شده را همراه مدارک و تاریخچهٔ ادغام دارد؛ MANIFEST داخل ZIP همچنان به 32e222e2 اشاره می‌کند. دامنهٔ این تحویل ویندوز با **اجرای یک نمونه در هر زمان** است. نصب و پذیرش تمیز مک تأیید نشده‌اند؛ نتیجهٔ تاریخی U1 mixed در rc2، U2 موفق و پذیرش زندهٔ اجرا‌نشدهٔ مک تغییر نکرده‌اند.
