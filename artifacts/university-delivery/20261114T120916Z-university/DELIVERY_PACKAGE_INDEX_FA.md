# فهرست بستهٔ تحویل دانشگاهی SolverScout

وضعیت پذیرش: **NOT_READY — ۹ PASS، پنج FAIL و یک NOT_RUN**.

- نصب کاندید: SolverScout_University_Delivery_2.1.0rc2.zip و checksum مجاور.
- گزارش فارسی: Final_University_Delivery_Report_FA.md.
- خلاصه و ماتریس: acceptance_summary.json و acceptance_matrix.csv.
- راهنمای نصب و ارائه: DELIVERY_GUIDE_FA.md و DEMO_GUIDE_FA.md.
- شواهد مستقل: SolverScout_University_Evidence_2.1.0rc2.zip و checksum مجاور.
- ورودی، JSON/HTML ذخیره‌شده، journal و مصرف: live/U1 و live/U2؛ شکست تلاش اول U2 محفوظ است.
- آزمون نهایی: offline/summary.json، run-manifest.json، junit.xml و pytest.log.
- نصب و برنامهٔ روشن: installation/final-runtime-verification.json؛ نشانی http://127.0.0.1:60865/.
- بررسی نصب: package-audit/final-package-verification.json و final-privacy-scan.json.
- بررسی ZIP شواهد: package-audit/evidence-package-verification.json و evidence-privacy-scan.json پس از ساخت، بیرون خود ZIP قرار دارند.

کد بسته و رگرسیون نهایی: 412ab7346f76d93ef3095ef14befa4a754e3f558، نسخهٔ rc2؛ hash اجرایی b6f9bcf1468276502e32792f1930a566c2a908414687e04e64c9c34e47769bd8.
پذیرش زنده روی rc1 انجام شده و با شکست U2 بسته است. اصلاح rc2 فقط آفلاین آزموده شده؛ موفقیت U1 قدیمی به این کد تازه نسبت داده نمی‌شود. ادغام با dev/main انجام نشده است.

manifest محتویات شواهد SHA هر فایل را ثبت می‌کند؛ خود manifest و بررسی‌های بیرونیِ پس از ساخت از فهرست خودارجاعی کنار گذاشته شده‌اند. checksum خود ZIP شواهد در فایل مجاور قرار دارد. بستهٔ نصب جداست و داخل ZIP شواهد تکرار نمی‌شود. snapshot عمومی مستندات و مجوز آن همراه شواهد است؛ کلیدها و دیتابیس خصوصی در بسته نیستند.
