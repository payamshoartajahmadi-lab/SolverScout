"""Configure the model and search connections through the local API."""

from __future__ import annotations

from uuid import uuid4

import streamlit as st
from pydantic import ValidationError

from p_mc_optimizer.llm.models import OPENAI_BASE_URL, ProfileUpdate, origin_of
from p_mc_optimizer.ui.web_research import search_settings

PROFILE_ROUTE = "/settings/llm/profile"
CREDENTIAL_ROUTE = "/settings/llm/credential"
TEST_ROUTE = "/settings/llm/test"
STORAGE_LABELS = {"session": "حافظهٔ همین نشست", "os_keyring": "مخزن امن سیستم‌عامل"}
TEST_LABELS = {
    "passed": "آزمون موفق",
    "failed": "آزمون ناموفق",
    "pending": "آزمون در جریان است",
    "not_tested": "آزمون نشده",
}


def model_connection_state(result):
    """A historical test never overrides the current credential status."""
    profile = (result or {}).get("profile") or {}
    credential = (result or {}).get("credential_status") or {}
    if not profile:
        return False, "نیاز به تنظیم سرویس"
    if not credential.get("available"):
        return False, "نیاز به کلید"
    test = profile.get("test_status")
    if test == "passed":
        return True, "آمادهٔ استفاده"
    return False, TEST_LABELS.get(test, "نیاز به آزمون اتصال")


def _target(result, job_id):
    profile = result["profile"]
    return {
        "profile_id": profile["profile_id"],
        "profile_revision": profile["revision"],
        "expected_credential_generation": result["credential_status"]["credential_generation"],
        "target_job_id": job_id,
    }


def _notice(key, level, message):
    st.session_state[f"{key}:notice"] = (level, message)
    st.rerun()


def _key_fields(result):
    credential = result.get("credential_status") or {}
    key = st.text_input("کلید API", type="password", autocomplete="off")
    choices = ["session", "os_keyring"] if result.get("os_keyring_available") else ["session"]
    current = credential.get("storage_mode")
    storage = st.selectbox("نگهداری کلید", choices, index=choices.index(current) if current in choices else 0,
                           format_func=STORAGE_LABELS.__getitem__)
    st.caption("کلید نشست با توقف برنامه از دسترس خارج می‌شود. روش نگهداری هنگام ثبت کلید اعمال می‌شود.")
    if credential.get("available"):
        st.caption("برای آزمون با کلید فعلی، ورودی کلید را خالی بگذارید.")
    return key.strip(), storage


def _advanced_fields(profile, preset):
    modes = ["json_schema", "json_object", "text_json"]
    fields = ["max_completion_tokens", "max_tokens"]
    with st.expander("تنظیمات پیشرفته"):
        mode = st.selectbox("شیوهٔ خروجی ساخت‌یافته", modes,
                            index=modes.index(profile.get("response_mode", modes[0])))
        field = st.selectbox("پارامتر سقف خروجی", fields, disabled=preset == "openai",
                             index=0 if preset == "openai" else fields.index(profile.get("token_limit_field", fields[0])))
        max_tokens = st.number_input("سقف توکن خروجی نقش", 256, 16384, profile.get("max_output_tokens", 4096))
        test_tokens = st.number_input("سقف توکن آزمون اتصال", 128, 4096,
                                      profile.get("connection_test_max_output_tokens", 512))
        timeout = st.number_input("مهلت پاسخ API (ثانیه)", 5, 300, profile.get("request_timeout_seconds", 60))
        if profile.get("tested_at"):
            last_test = TEST_LABELS.get(profile.get("test_status"), "ثبت‌شده")
            st.caption(f"آخرین آزمون ثبت‌شده: {last_test} · {profile['tested_at']}")
    return {
        "response_mode": mode, "token_limit_field": field, "max_output_tokens": max_tokens,
        "connection_test_max_output_tokens": test_tokens, "request_timeout_seconds": timeout,
    }


def _test(attempt, result, key, job_id, secret, storage, *, changed=False):
    target = _target(result, job_id)
    if secret:
        saved = attempt("PUT", CREDENTIAL_ROUTE, {**target, "api_key": secret, "storage_mode": storage})
        if not saved:
            if changed:
                _notice(key, "warning", "تنظیمات سرویس ثبت شد. ثبت کلید انجام نشد. کلید را دوباره بررسی کنید.")
            return
        changed = True
        target["expected_credential_generation"] = saved["credential_generation"]
    with st.spinner("در حال آزمون اتصال مدل…"):
        test = attempt("POST", TEST_ROUTE, target, idempotency_key=str(uuid4()))
    if test and test.get("test_status") == "passed":
        _notice(key, "success", "آزمون اتصال مدل موفق بود.")
    elif test:
        error = test.get("error") or {}
        _notice(key, "error", error.get("message_fa") or "آزمون اتصال مدل موفق نبود.")
    elif changed:
        _notice(key, "warning", "تنظیمات ثبت شد. نتیجهٔ آزمون دریافت نشد. وضعیت اتصال را بررسی کنید.")


def _configure_model(attempt, result, key):
    profile = result.get("profile") or {}
    credential = result.get("credential_status") or {}
    revision = profile.get("revision")
    generation = credential.get("credential_generation")
    preset = st.selectbox(
        "ارائه‌دهنده", ["openai", "custom_openai_compatible"],
        index=int(profile.get("preset") == "custom_openai_compatible"), key=f"{key}:preset:{revision}",
        format_func=lambda name: "OpenAI" if name == "openai" else "سازگار با OpenAI",
    )
    with st.form(f"{key}:profile:{revision}:{generation}:{preset}", border=False,
                 clear_on_submit=True, enter_to_submit=False):
        model = st.text_input("شناسهٔ مدل", value=profile.get("model", ""), max_chars=256,
                              help="شناسهٔ دقیق مدل را از سرویس ارائه‌دهنده وارد کنید.")
        base_url = st.text_input(
            "نشانی سرویس", value=OPENAI_BASE_URL if preset == "openai" else (
                profile.get("base_url", "") if profile.get("preset") == preset else ""),
            placeholder="https://api.example.com/v1", disabled=preset == "openai", max_chars=2048,
        )
        secret, storage = _key_fields(result)
        st.caption("آزمون اتصال یک درخواست کوتاه به نشانی بالا می‌فرستد و ممکن است اعتبار API مصرف کند.")
        with st.container(horizontal=True):
            save_only = st.form_submit_button("فقط ثبت تنظیمات سرویس")
            test = st.form_submit_button("ثبت و آزمون اتصال", type="primary")
        advanced = _advanced_fields(profile, preset)
    if not (save_only or test):
        return
    try:
        update = ProfileUpdate(expected_revision=revision, preset=preset, base_url=base_url.strip(),
                               model=model.strip(), **advanced).model_dump(mode="json")
    except ValidationError as error:
        if any("base_url" in item["loc"] for item in error.errors(include_input=False)):
            st.error("نشانی پایهٔ سرویس را با HTTPS وارد کنید. مسیر chat/completions را اضافه نکنید.")
        else:
            st.error("شناسهٔ دقیق مدل را وارد کنید و تنظیمات سرویس را بررسی کنید.")
        return
    same_origin = bool(profile and origin_of(update["base_url"]) == origin_of(profile["base_url"]))
    if test and not secret and not (same_origin and credential.get("available")):
        st.error("برای آزمون اتصال، کلید API همین سرویس را وارد کنید.")
        return
    changed = any(profile.get(field) != value for field, value in update.items() if field != "expected_revision")
    if changed:
        saved = attempt("PUT", PROFILE_ROUTE, update)
        if not saved:
            return
        if (test and same_origin and saved["credential_status"]["credential_generation"] != generation):
            _notice(key, "warning", "تنظیمات سرویس ثبت شد. کلید در نشست دیگری تغییر کرده است. اتصال را دوباره بررسی کنید.")
        result = saved
    if save_only:
        _notice(key, "success", "تنظیمات سرویس ثبت شد.")
    _test(attempt, result, key, None, secret, storage, changed=changed)


def _repair_model(attempt, result, key, job_id, job_version, ready):
    profile = result["profile"]
    st.write(f"مدل این اجرا: {profile['model']}")
    st.code(profile["base_url"], language=None)
    available = result["credential_status"].get("available")
    with st.form(f"{key}:credential:{profile['revision']}:{result['credential_status']['credential_generation']}",
                 border=False, clear_on_submit=True, enter_to_submit=False):
        secret, storage = _key_fields(result)
        st.caption("آزمون اتصال ممکن است اعتبار API مصرف کند.")
        test = st.form_submit_button("ثبت و آزمون اتصال", type="secondary" if ready else "primary")
    if test:
        if secret or available:
            _test(attempt, result, key, job_id, secret, storage)
        else:
            st.error("کلید API همین سرویس را وارد کنید.")
    if st.button("ادامهٔ همین اجرا", type="primary" if ready else "secondary", disabled=not ready,
                 key=f"{key}:resume"):
        if attempt("POST", f"/jobs/{job_id}/resume", {"expected_job_version": job_version}):
            st.rerun()


def model_settings(attempt, job_id=None, job_version=None):
    key = f"llm-connection:{job_id or 'active'}"
    st.subheader("مدل زبانی" if job_id is None else "رفع مشکل اتصال مدل این اجرا")
    result = attempt("GET", PROFILE_ROUTE if job_id is None else f"{PROFILE_ROUTE}?job_id={job_id}")
    if result is None:
        return False
    if notice := st.session_state.pop(f"{key}:notice", None):
        getattr(st, notice[0])(notice[1])
    ready, label = model_connection_state(result)
    st.markdown(f"**{label}**")
    credential = result.get("credential_status") or {}
    if label == "نیاز به کلید":
        if credential.get("storage_mode") == "session":
            st.caption("کلید این نشست در دسترس نیست. دوباره وارد کنید.")
        else:
            st.caption("کلید API سرویس مدل را وارد کنید.")
    if job_id is None:
        _configure_model(attempt, result, key)
    elif result.get("profile"):
        _repair_model(attempt, result, key, job_id, job_version, ready)
    if credential.get("configured"):
        with st.expander("مدیریت کلید ذخیره‌شدهٔ مدل"):
            st.caption(f"شیوهٔ فعلی: {STORAGE_LABELS.get(credential.get('storage_mode'), 'نامشخص')}")
            if st.button("حذف کلید مدل", key=f"{key}:delete"):
                if attempt("DELETE", CREDENTIAL_ROUTE, _target(result, job_id)):
                    st.rerun()
    return ready


def connections_page(attempt, on_back=None, focus=None):
    if on_back is not None:
        st.button("بازگشت به مسئله", key="connections-back", on_click=on_back)
    st.title("اتصال‌های برنامه")
    st.caption("اتصال مدل زبانی و جست‌وجوی وب را آماده کنید.")
    model, search = st.columns([2, 1], gap="large")
    with model, st.container(key="ux-model-connection"):
        model_settings(attempt)
    with search, st.container(key="ux-search-connection"):
        search_settings(attempt, compact=True, expanded=focus in {"search", "tavily"})
