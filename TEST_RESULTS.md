# گزارش اجرای آزمون

تاریخ محیط کاربر ۲۰۲۶-۱۱-۰۹. جزئیات دستگاه و نسخه‌ها در [محیط](docs/ENVIRONMENT.md) آمده‌اند. پاسخ‌های مدل در آزمون‌های پیش‌فرض با برچسب TEST FIXTURE به transport داده شدند. ارزیابی‌های عددی در همان آزمون‌ها واقعی‌اند.

| فرمان یا بررسی | نتیجه |
| --- | --- |
| `.venv/bin/python -m pytest -q` | PASS، ۱۲۱ آزمون موفق، صفر شکست، ۱ آزمون زنده deselected، ۴۸٫۵۷ ثانیه |
| `.venv/bin/python -m pip check` | PASS، وابستگی شکسته گزارش نشد |
| `.venv/bin/python -m pytest -q tests/test_numeric.py` | PASS، ۶۷ مورد در اجرای متمرکز |
| `.venv/bin/python -m pytest -q tests/test_persistence_acceptance.py` | PASS، ۱۱ مورد از جمله مرگ Worker در loopback، بازیابی و لغو فعال |
| `.venv/bin/python -m pytest -q tests/test_llm_gateway.py tests/test_llm_workflow.py` | PASS، ۳۲ مورد، ۱ آزمون زنده به‌طور پیش‌فرض deselected |
| `solve --problem examples/quadratic_2d_max.json --policy fixed_solver --solver de.original --budget 1002` | PASS، ۱۰۰۲ FE، هدف اصلی 6.999784822372764، `valid_best_found`، `not_certified` |
| `benchmark --manifest examples/benchmark_smoke.json` | PASS، ۱۶ Job از ۱۶ با جواب اعتبارسنجی‌شده، صفر فراخوانی مدل |
| `pytest -o addopts='' -m live_provider -q tests/test_llm_workflow.py -rs` | BLOCKED، ۱ آزمون زنده skip شد. برای آزمون پروفایل غیرمحرمانه و کلید تعاملی لازم است |
| `pytest -q tests/test_workflow_exports.py tests/test_benchmarks.py` | PASS برای بیشینه‌سازی تا گزارش، CSV/HTML امن، سناریوهای شکست پایلوت و main، قالب نمونه و ثبت صریح مقایسهٔ پژوهشی |
| Chromium 158 / Playwright 1.65.0 روی loopback | PASS، مشاهدهٔ صفحهٔ آغاز، تنظیم و حذف کلید مصنوعی بدون اتصال، نمونهٔ درجه‌دو، حفظ تب نتایج، دانلود HTML، صفحهٔ آغاز در ۱۴۴۰×۹۰۰ و ۳۹۰×۸۴۴ |
| ساخت، بازرسی و نصب ZIP استخراج‌شده | PASS، macOS arm64، مسیر نصب و داده با نام فارسی و فاصله، بررسی SHA-256 هر فایل |
| wrapperهای start، doctor و stop پس از نصب تازه | PASS، شروع تکراری همان instance، پورت خارجی حفظ شد، گزارش پیشین پس از نصب تازه با hash یکسان باقی ماند |

## اجرای پژوهشی دودزا

چهار خانوادهٔ Sphere، Rosenbrock، Rastrigin و Ackley در دو بعد، با دو بذر ۱۴۰۵ و ۱۴۰۶، و سیاست‌های `fixed_solver` و `pilot_only` آزمایش شدند. هر Job دقیقاً ۷۰۰۲ FE مصرف کرد. مجموع ۱۱۲۰۳۲ FE و زمان عددی ثبت‌شدهٔ مجموع ۱۴۰٫۶۳۴ ثانیه بود. اجرای دوم پس از یکپارچه‌سازی سیاست‌ها دوباره ۱۶ جواب معتبر و ۱۱۲۰۳۲ FE با مجموع ۱۴۸٫۸۷۹ ثانیهٔ فعال ثبت کرد. هر ۱۶ نتیجه از دو ارزیابی بررسی نهایی گذشت. این مجموعهٔ کوچک آزمون صحت گردش‌کار است. مجموعهٔ پژوهشی بزرگ ۳۶ مسئله و ۲۰ بذر دارد. مدل‌ها، hashها و مراجع تحلیلی اعتبارسنجی شدند. کل مجموعه اجرا نشد.

SHA-256 فایل manifest دودزا

```
4761647d07d08892ffff7bc0b0b67a95b7b5f0020430651b6ce66cae044752d4
```

## بازبینی رابط و بسته

Browser plugin در محیط آزمون موجود نبود و آزمون در Chromium با Playwright اجرا شد. عنوان و نشانی صفحه، محتوای اولیه، نبود خطای چارچوب و کنسول، نبود درخواست مرورگر به خارج از loopback، پاک‌شدن فیلد password پس از ثبت و دانلود گزارش بررسی شدند. نسخهٔ نخست PyArrow در مرورگر هنگام نمایش جدول با NumPy 1.26 خطا داد. نسخهٔ 23.0.1 قفل شد. پس از اصلاح بازخوانی تب و نمودار، آزمون مرورگر بدون خطای کنسول یا هشدار نمودار گذشت.

ZIP در پوشهٔ تازه و فارسی استخراج شد و wrapper `setup.sh`، `pip check`، `doctor` و حل DE را اجرا کرد. نصب دوباره روی سورس تازه با همان مسیر داده، رکورد و hash فایل‌های گزارش را حفظ کرد. آزمایش rollback و backup مهاجرت در suite ذخیره‌سازی جدا ثبت شده است. داده‌ها و profile مصنوعی آزمایش رابط خارج از ZIP هستند.

## پلتفرم‌ها

| پلتفرم | اجرا |
| --- | --- |
| macOS 27.0.1 arm64، Python 3.12.16 | PASS برای هسته، loopback، رابط، نصب تازه و اجرای نمونه |
| Windows 11 | NOT_TESTED |
| Linux | NOT_TESTED |
| macOS Intel | NOT_TESTED |

آزمون زنده با نشانگر `live_provider` از آزمون‌های استاندارد جداست. روش فعال‌سازی در [آزمون زنده](docs/LIVE_PROVIDER_FA.md) آمده است.
