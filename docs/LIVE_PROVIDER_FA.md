# آزمون اتصال واقعی مدل

در برنامه، «تنظیمات مدل زبانی» را باز کنید. نشانی پایهٔ HTTPS، شناسهٔ دقیق مدل، روش خروجی و نام فیلد سقف توکن را از حساب و مستندات ارائه‌دهنده بردارید. پس از ذخیرهٔ تنظیمات، کلید را در فیلد گذرواژه وارد و ثبت کنید. «آزمون اتصال» یک درخواست کوتاه به همان مدل می‌فرستد و ممکن است هزینه داشته باشد. سپس نمونهٔ متن فارسی را از صفحهٔ مسئلهٔ جدید اجرا کنید تا صورت‌بندی، پیشنهاد و توضیح با مدل واقعی سنجیده شوند.

آزمون خودکار اختیاری زیر یک سرویس آزمایشی روی loopback راه می‌اندازد و از مسیر عادی backend به ارائه‌دهنده وصل می‌شود. برای اجرای آن یک فایل JSON محلی **بدون کلید** شامل این فیلدها آماده کنید.

- `preset` با مقدار `openai` یا `custom_openai_compatible`
- `base_url` با نشانی پایهٔ HTTPS، بدون پسوند `/chat/completions`
- `model` با شناسهٔ دقیق مدل حساب شما
- `response_mode` با مقدار `json_schema`، `json_object` یا `text_json`
- `token_limit_field` با مقدار `max_completion_tokens` یا `max_tokens`
- فیلدهای اختیاری `max_output_tokens`، `connection_test_max_output_tokens` و `request_timeout_seconds`

فرمان را در ترمینال تعاملی اجرا کنید. کلید را فقط در درخواست مخفی ترمینال وارد کنید. این مسیر یک تماس آزمون اتصال و حداکثر شش تماس نقش‌های حل دارد.

```sh
PMC_TEST_LIVE_PROVIDER_CONFIG="/path/to/profile.json" .venv/bin/python -m pytest -s -o addopts='' -m live_provider tests/test_llm_workflow.py
```

در Windows، متغیر را با `$env:PMC_TEST_LIVE_PROVIDER_CONFIG` در PowerShell تنظیم و `.venv\Scripts\python.exe` را اجرا کنید. فایل پروفایل را خارج از سورس یا ZIP نگه دارید. بدون فایل تنظیمات و ترمینال تعاملی، آزمون با وضعیت `BLOCKED` رد می‌شود. آزمون‌های عادی از `TEST FIXTURE` کنترل‌شده استفاده می‌کنند. نتیجهٔ آن‌ها جدا از اتصال و کیفیت مدل واقعی ثبت می‌شود.

مرجع‌های قالب درخواست و خروجی ساخت‌یافته در مستندات رسمی OpenAI موجودند. [Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)، [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)
