"""Local source library controls and refresh status."""

from __future__ import annotations

from datetime import datetime
from math import ceil
from urllib.parse import urlencode
from uuid import uuid4

import streamlit as st

from p_mc_optimizer.research.contracts_v21 import LibrarySourceUpdate, SyncLimits

LIBRARY_SOURCES_ROUTE = "/library/sources"
SYNC_RUNS_ROUTE = "/library/sync-runs"
RETRIEVAL_ROUTE = "/settings/retrieval"
RETRIEVAL_MODES = {
    "local_only": "کتابخانهٔ محلی",
    "direct_refresh": "به‌روزرسانی منابع و حل",
    "web_search": "کشف وب با موتور انتخابی",
}
STALE_POLICIES = {
    "reject": "فقط شواهد تازه",
    "allow_with_warning": "شواهد قدیمی با هشدار",
}
SYNC_LABELS = {
    "queued": "در صف",
    "running": "در حال دریافت",
    "completed": "انجام شد",
    "partial": "بخشی از منابع به‌روز شد",
    "failed": "ناموفق",
    "cancelled": "لغوشده",
    "interrupted": "قطع‌شده",
}
SYNC_LIMITS = (
    ("max_sources", "تعداد منابع", 1, 10),
    ("max_urls_per_source", "نشانی‌های کشف‌شده در هر منبع", 1, 1000),
    ("max_documents_per_source", "تلاش دریافت سند در هر منبع", 1, 100),
    ("max_http_requests_per_source", "درخواست HTTP در هر منبع", 1, 400),
    ("max_source_seconds", "مهلت هر منبع (ثانیه)", 1.0, 300.0),
    ("max_active_seconds", "مهلت کل به‌روزرسانی (ثانیه)", 1.0, 600.0),
    ("request_timeout_seconds", "مهلت هر درخواست (ثانیه)", 1.0, 30.0),
    ("max_document_bytes", "حجم هر سند (بایت)", 1, 10 * 1024**2),
    ("max_sitemap_bytes", "حجم هر نقشهٔ سایت (بایت)", 1, 5 * 1024**2),
    ("max_total_bytes", "حجم کل (بایت)", 1, 100 * 1024**2),
    ("max_text_chars", "نویسهٔ متن هر سند", 1, 200_000),
    ("max_redirects", "تغییر مسیر هر درخواست", 0, 3),
)


def display_timestamp(value):
    if not value:
        return "—"
    try:
        shown = datetime.fromisoformat(value).astimezone().strftime("%Y/%m/%d · %H:%M %Z")
    except ValueError:
        shown = value
    return f"\u2066{shown}\u2069"


def sync_limit_fields(key, defaults=None, *, field=None):
    defaults = SyncLimits().model_dump() | (defaults or {})
    values = {}
    columns = st.columns(2)
    for index, (name, label, minimum, maximum) in enumerate(SYNC_LIMITS):
        initial = type(minimum)(defaults[name])
        widget = columns[index % 2].number_input
        if field:
            values[name] = field(widget, f"{key}-{name}", label, minimum, maximum, default=initial)
        else:
            values[name] = widget(label, minimum, maximum, initial, key=f"{key}-{name}")
    return values


def sync_limit_caption(limits):
    st.caption(
        f"سقف دریافت هر منبع {limits['max_documents_per_source']} سند · "
        f"مهلت کل {limits['max_active_seconds']:g} ثانیه · "
        f"حجم کل {limits['max_total_bytes'] / 1024**2:g} MiB"
    )


def retrieval_settings(attempt):
    settings = attempt("GET", RETRIEVAL_ROUTE)
    if settings is None:
        return False
    st.subheader("موتور جست‌وجوی وب")
    revision = settings["revision"]
    with st.form(f"retrieval-settings:{revision}"):
        enabled = st.selectbox(
            "موتور جست‌وجو", [False, True], index=int(settings["web_search_enabled"]),
            format_func=lambda choice: "Tavily" if choice else "غیرفعال",
        )
        modes = list(RETRIEVAL_MODES)
        mode = st.selectbox(
            "روش پیش‌فرض پژوهش", modes, index=modes.index(settings["retrieval_mode"]),
            format_func=RETRIEVAL_MODES.__getitem__,
        )
        age = st.number_input("حداکثر عمر شواهد (ساعت)", 1, 8760, settings["max_evidence_age_hours"])
        stale_choices = list(STALE_POLICIES)
        stale = st.selectbox(
            "سیاست تازگی شواهد", stale_choices, index=stale_choices.index(settings["stale_evidence_policy"]),
            format_func=STALE_POLICIES.__getitem__,
        )
        save = st.form_submit_button("ثبت تنظیمات پژوهش")
    if save:
        if mode == "web_search" and not enabled:
            st.error("برای انتخاب جست‌وجوی وب به‌عنوان روش پیش‌فرض، موتور جست‌وجو را فعال کنید.")
        elif attempt("PUT", RETRIEVAL_ROUTE, {
            "expected_revision": revision, "web_search_enabled": enabled,
            "retrieval_mode": mode, "max_evidence_age_hours": age,
            "stale_evidence_policy": stale, "on_research_failure": settings["on_research_failure"],
        }):
            st.rerun()
    if not settings["web_search_enabled"]:
        st.caption("جست‌وجوی وب غیرفعال است. از کتابخانهٔ محلی یا دریافت مستقیم استفاده کنید.")
    return settings["web_search_enabled"]


def _source_summary(entry):
    rule = entry["rule"]
    st.text(f"{rule['label']} · {entry.get('usable_documents', 0)} سند قابل استفاده")
    st.caption(f"آخرین بررسی موفق · {display_timestamp(entry.get('last_successful_validation_at'))}")
    st.caption(f"آخرین تلاش · {display_timestamp(entry.get('last_attempt_at'))}")
    if error := entry.get("last_error"):
        st.text(f"نتیجهٔ آخرین تلاش · {error}")
    if entry.get("needs_review"):
        st.warning("مجوز منبع تغییر کرده است. محدودهٔ دریافت را بازبینی و دوباره ثبت کنید.")


def _lines(value):
    return [line.strip() for line in value.splitlines() if line.strip()]


def _source_form(attempt, entry):
    rule, config = entry["rule"], entry.get("config") or {}
    source_id = rule["source_id"]
    revision = config.get("revision", 0)
    prefix = f"library-source:{source_id}:{revision}"
    st.caption("دریافت سند از این دامنه و مسیرهای مجاز انجام می‌شود.")
    st.code("\n".join(rule["hostname"] + path for path in rule["path_prefixes"]), language=None)
    with st.form(prefix):
        seeds = st.text_area("نشانی آغازین سند، هر نشانی در یک سطر", "\n".join(config.get("seed_urls", [])))
        sitemaps = st.text_area(
            "نشانی نقشهٔ سایت، هر نشانی در یک سطر", "\n".join(config.get("sitemap_urls", [])),
            help="نقشهٔ سایت باید روی همین دامنه باشد. فقط نشانی اسناد مجاز از آن خوانده می‌شود.",
        )
        follow = st.checkbox("دنبال‌کردن پیوندهای مجاز", value=config.get("follow_links", True))
        depth = st.number_input("عمق پیمایش از صفحهٔ آغازین", 0, 2, config.get("crawl_depth", 1))
        with st.expander("سقف اختصاصی این منبع"):
            custom = st.checkbox("ثبت سقف اختصاصی", value=config.get("limits") is not None)
            limits = sync_limit_fields(prefix, config.get("limits"))
        save = st.form_submit_button("ثبت تنظیمات دریافت منبع")
    if save:
        try:
            update = LibrarySourceUpdate(
                expected_revision=revision, seed_urls=_lines(seeds), sitemap_urls=_lines(sitemaps),
                follow_links=follow, crawl_depth=depth, limits=limits if custom else None,
            )
        except ValueError:
            st.error("نشانی‌های آغازین یا نقشهٔ سایت را بررسی کنید. دست‌کم یک نشانی HTTPS معتبر لازم است.")
        else:
            if attempt("PUT", f"{LIBRARY_SOURCES_ROUTE}/{source_id}", update.model_dump(mode="json")):
                st.rerun()


def sync_run_panel(attempt, run_id, *, allow_cancel=False):
    run = attempt("GET", f"{SYNC_RUNS_ROUTE}/{run_id}")
    if not run:
        return
    status = run["status"]
    st.text(f"به‌روزرسانی {run_id} · {SYNC_LABELS.get(status, status)}")
    usage = run.get("usage") or {}
    st.caption(
        f"{usage.get('http_requests', 0)} درخواست · "
        f"{usage.get('expanded_bytes', 0):,} بایت دریافت‌شده · "
        f"{usage.get('unknown_bytes', 0):,} بایت با وضعیت نامعلوم"
    )
    if summaries := run.get("sources"):
        st.dataframe([{
            "منبع": row["source_id"], "بررسی‌شده": row.get("documents_checked", 0),
            "تازه": row.get("new", 0), "تغییریافته": row.get("changed", 0),
            "بدون تغییر": row.get("unchanged", 0), "ناموفق": row.get("failed", 0),
            "پوشش محدود": row.get("budget_limited", False),
            "علت": " · ".join(row.get("reason_codes", [])),
        } for row in summaries], hide_index=True)
    if reasons := run.get("reason_codes"):
        st.text(" · ".join(reasons))
    if status in {"queued", "running"}:
        if run.get("cancel_requested"):
            st.info("درخواست لغو ثبت شد.")
        elif allow_cancel and st.button("لغو به‌روزرسانی", key=f"cancel-sync:{run_id}"):
            if attempt("POST", f"{SYNC_RUNS_ROUTE}/{run_id}/cancel", {"expected_version": run["version"]}):
                st.rerun()
        st.button("خواندن وضعیت تازه", key=f"refresh-sync:{run_id}")


def _refresh_controls(attempt, entries):
    st.subheader("به‌روزرسانی منابع")
    configured = {
        entry["rule"]["source_id"]: entry["rule"]["label"]
        for entry in entries if entry.get("config") and not entry.get("needs_review")
    }
    with st.expander("محدودیت‌های به‌روزرسانی"):
        limits = sync_limit_fields("library-refresh")
    selected = st.multiselect(
        "منابع برای به‌روزرسانی", list(configured), format_func=configured.__getitem__,
        max_selections=limits["max_sources"], placeholder="منابع را انتخاب کنید",
    )
    sync_limit_caption(limits)
    if st.button("شروع به‌روزرسانی", type="primary", disabled=not selected):
        payload = {"source_ids": selected, "limits": limits}
        pending = st.session_state.get("library-sync-pending")
        if not pending or pending["payload"] != payload:
            pending = {"payload": payload, "key": str(uuid4())}
            st.session_state["library-sync-pending"] = pending
        if run := attempt("POST", SYNC_RUNS_ROUTE, payload, idempotency_key=pending["key"]):
            st.session_state.pop("library-sync-pending", None)
            st.session_state["library-sync-selection"] = run["run_id"]
            st.rerun()
    if history := attempt("GET", SYNC_RUNS_ROUTE):
        independent = {row["run_id"]: row for row in history["runs"] if not row.get("owner_job_id")}
        if independent:
            selected_run = st.selectbox(
                "به‌روزرسانی‌های ثبت‌شده", list(independent), key="library-sync-selection",
                format_func=lambda item: f"{item[:8]} · {SYNC_LABELS.get(independent[item]['status'], independent[item]['status'])}",
            )
            sync_run_panel(attempt, selected_run, allow_cancel=True)


def _documents(attempt, entries):
    st.subheader("اسناد کتابخانه")
    labels = {entry["rule"]["source_id"]: entry["rule"]["label"] for entry in entries}
    source = st.selectbox("فیلتر منبع", [None, *labels], format_func=lambda item: labels.get(item, "همهٔ منابع"))
    query = st.text_input("جست‌وجو در متن اسناد", max_chars=300).strip()
    if query:
        data = {"query": query, "limit": 20}
        if source:
            data["source_ids"] = [source]
        response = attempt("POST", "/library/search", data)
    else:
        page = st.number_input("صفحهٔ اسناد", 1, step=1)
        params = {"limit": 20, "offset": 20 * (page - 1)}
        if source:
            params["source_id"] = source
        response = attempt("GET", f"/library/documents?{urlencode(params)}")
    if response is None:
        return
    documents = response["documents"]
    if not documents:
        st.info("سندی برای این انتخاب پیدا نشد.")
        return
    options = {row["document_id"]: row for row in documents}
    selected = st.selectbox(
        "سند", list(options), index=None, placeholder="سندی برای مشاهده انتخاب کنید",
        format_func=lambda key: options[key].get("title") or options[key]["canonical_url"],
    )
    if not selected:
        return
    item = options[selected]
    st.text(item["canonical_url"])
    st.caption(f"آخرین اعتبارسنجی · {display_timestamp(item.get('validated_at'))}")
    if item.get("stale"):
        st.warning("این سند طبق سقف پیش‌فرض تازگی قدیمی است.")
    if item.get("preview"):
        st.text(item["preview"])
    history = attempt("GET", f"/library/documents/{selected}/versions")
    if not history or not history["versions"]:
        return
    versions = {row["version_id"]: row for row in history["versions"]}
    version_id = st.selectbox("نسخهٔ سند", list(versions), format_func=lambda item: item[:12])
    version = versions[version_id]
    st.text(f"هش متن · {version['text_sha256']}")
    if version.get("truncated"):
        st.warning("استخراج متن این نسخه ناقص است.")
    if version.get("text") is None:
        st.warning("متن این نسخه در دسترس نیست.")
    else:
        with st.container(height=250):
            st.text(version["text"])
    with st.expander("سابقهٔ دریافت و اعتبارسنجی"):
        st.json(version["observations"], expanded=False)


def _storage_controls(attempt):
    with st.expander("فضای ذخیره‌سازی کتابخانه"):
        if usage := attempt("GET", "/library/storage"):
            mib = 1024**2
            st.text(f"{usage['used_bytes'] / mib:.2f} MiB استفاده‌شده از {usage['limit_bytes'] / mib:g} MiB")
            with st.form(f"library-storage:{usage['limit_bytes']}"):
                limit = st.number_input("سقف فضای کتابخانه (MiB)", 1, 10 * 1024, ceil(usage["limit_bytes"] / mib))
                save = st.form_submit_button("ثبت سقف فضا")
            if save and attempt("PUT", "/library/storage", {"limit_bytes": limit * mib}):
                st.rerun()
            if st.button("پاک‌سازی فایل‌های بی‌ارجاع"):
                if result := attempt("POST", "/library/storage/cleanup"):
                    st.success(f"{result['removed']} فایل بی‌ارجاع پاک شد.")
        if st.button("واردکردن اسناد ذخیره‌شدهٔ نسخهٔ ۲"):
            if result := attempt("POST", "/library/import-legacy"):
                st.success(f"{result['verified']} سند بررسی شد و {result['skipped']} سند کنار گذاشته شد.")


def library_page(attempt):
    response = attempt("GET", LIBRARY_SOURCES_ROUTE)
    if response is None:
        return
    entries = [entry for entry in response["sources"] if entry["rule"]["enabled"]]
    st.header("کتابخانهٔ منابع")
    if not entries:
        st.info("ابتدا در برگهٔ «مجوز منابع» یک منبع فعال ثبت کنید.")
        return
    st.caption("اسناد از منابع مجاز دریافت می‌شوند و در کتابخانهٔ محلی می‌مانند.")
    available = sum(entry.get("usable_documents", 0) for entry in entries)
    if available:
        st.caption(f"کتابخانه · {available} سند ذخیره‌شده")
    for entry in entries:
        _source_summary(entry)
    with st.expander("تنظیم دریافت مستقیم"):
        by_id = {entry["rule"]["source_id"]: entry for entry in entries}
        selected = st.selectbox(
            "منبع برای تنظیم", list(by_id), format_func=lambda key: by_id[key]["rule"]["label"],
        )
        _source_form(attempt, by_id[selected])
    _refresh_controls(attempt, entries)
    _documents(attempt, entries)
    _storage_controls(attempt)
