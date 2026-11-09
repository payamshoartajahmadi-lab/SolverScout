"""Local controls and read-only displays for bounded web research."""

from __future__ import annotations

import json
from uuid import uuid4

import pandas as pd
import streamlit as st

from p_mc_optimizer.domain.models import strict_json_loads
from p_mc_optimizer.research.contracts import ResearchLimits, SourceRule

SEARCH_ROUTE = "/settings/search"
SOURCES_ROUTE = "/settings/research-sources"
SOURCE_KINDS = ("official_docs", "paper_repository", "code_repository", "benchmark_report")
RESEARCH_STATUS = {
    "not_requested": "درخواست نشده",
    "pending": "در انتظار شروع",
    "running": "در حال پژوهش",
    "completed": "دریافت شواهد انجام شد",
    "partial": "دریافت بخشی از شواهد",
    "insufficient_evidence": "شواهد ناکافی",
    "failed": "پژوهش ناموفق",
    "cancelled": "لغوشده",
}
CONTENT_LABELS = {
    "snippet": "خلاصهٔ موتور جست‌وجو",
    "abstract": "چکیده",
    "documentation": "مستندات یا متن بخشی از صفحه",
    "full_text": "متن کامل قابل‌دریافت",
}
SEARCH_BLOCKERS = {
    "SEARCH_CONFIGURATION_REQUIRED",
    "SEARCH_AUTH_FAILED",
    "SEARCH_RATE_LIMITED",
    "RESEARCH_EVIDENCE_INSUFFICIENT",
}


def profile_reference(profile):
    if profile:
        return {"profile_id": profile["profile_id"], "revision": profile["revision"]}
    return None


def search_settings(attempt, *, repair=False):
    st.header("اتصال جست‌وجوی وب" if not repair else "رفع مشکل جست‌وجوی وب")
    result = attempt("GET", SEARCH_ROUTE)
    if result is None:
        return
    profile = result.get("profile") or {}
    st.caption("پژوهش زنده از Tavily استفاده می‌کند و کلید آن جدا از کلید مدل زبانی است.")
    if not profile:
        st.info("ابتدا تنظیمات جست‌وجو را ثبت کنید.")
        return False
    target = {"revision": profile["revision"], "generation": result["credential_generation"]}
    available = result.get("credential_available", False)
    ready = bool(result.get("ready"))
    if ready:
        st.success("اتصال Tavily آماده است.")
    elif available:
        st.info("کلید ثبت شده است. برای آماده شدن جست‌وجوی زنده، اتصال را آزمایش کنید.")
    else:
        st.info("برای جست‌وجوی زنده، ابتدا کلید API سرویس Tavily را ثبت کنید.")

    storage_options = ["session", "os_keyring"] if result.get("os_keyring_available") else ["session"]
    guard = f"{profile['profile_id']}:{target['revision']}:{target['generation']}"
    with st.expander("مدیریت کلید Tavily", expanded=repair or not available):
        with st.form(f"search-credential:{guard}", clear_on_submit=True):
            secret = st.text_input("کلید API جست‌وجو", type="password", autocomplete="off")
            storage = st.selectbox(
                "شیوهٔ نگهداری کلید جست‌وجو", storage_options,
                format_func=lambda option: "حافظهٔ نشست" if option == "session" else "مخزن امن سیستم‌عامل",
            )
            save_secret = st.form_submit_button("ثبت کلید جست‌وجو", type="secondary" if available else "primary")
        if save_secret:
            if not secret.strip():
                st.error("کلید جست‌وجو را وارد کنید.")
            elif attempt(
                "PUT", f"{SEARCH_ROUTE}/credential",
                {**target, "secret": secret, "storage_mode": storage}, idempotency_key=str(uuid4()),
            ):
                st.rerun()
        if result.get("storage_mode") and st.button("حذف کلید جست‌وجو", key=f"delete-search:{guard}"):
            if attempt("DELETE", f"{SEARCH_ROUTE}/credential", target, idempotency_key=str(uuid4())):
                st.rerun()

    if st.button(
        "آزمون اتصال جست‌وجو", type="secondary" if ready or not available else "primary",
        disabled=not available, key=f"test-search:{guard}",
        help="یک پرس‌وجوی آزمایشی به Tavily می‌فرستد و ممکن است اعتبار مصرف کند.",
    ):
        if test := attempt("POST", f"{SEARCH_ROUTE}/test", target, idempotency_key=str(uuid4())):
            ready = bool(test.get("ok"))
            if ready:
                st.success(f"اتصال برقرار شد. تعداد نتایج مجاز آزمون: {test.get('allowed_results', 0)}")
            else:
                st.error(f"آزمون اتصال ناموفق بود. {test.get('code') or test.get('status', '')}")
            if test.get("retry_after"):
                st.text(f"زمان اعلام‌شده برای تلاش دوباره · {test['retry_after']}")

    if not repair:
        with st.expander("تنظیمات پیشرفتهٔ جست‌وجو"):
            _search_limits_form(attempt, profile)
    return ready


def _search_limits_form(attempt, profile):
    limits = (profile.get("limits") or ResearchLimits().model_dump()).copy()
    limit_fields = [
        ("max_search_attempts", "تلاش جست‌وجو", 1, 3),
        ("max_queries", "پرس‌وجوی هر اجرا", 1, 3),
        ("max_results_per_query", "نتیجهٔ هر پرس‌وجو", 1, 5),
        ("max_documents", "تعداد سند", 1, 6),
        ("max_http_requests", "درخواست HTTP", 1, 27),
        ("max_redirects", "تغییر مسیر هر سند", 0, 3),
        ("request_timeout_seconds", "زمان هر درخواست (ثانیه)", 1.0, 30.0),
        ("active_timeout_seconds", "زمان فعال پژوهش (ثانیه)", 1.0, 180.0),
    ]
    with st.form(f"search-profile:{profile['revision']}"):
        columns = st.columns(2)
        for index, (field, label, minimum, maximum) in enumerate(limit_fields):
            limits[field] = columns[index % 2].number_input(
                label, minimum, maximum, type(minimum)(limits[field]),
            )
        save = st.form_submit_button("ثبت محدودیت‌های جست‌وجو")
    if save and attempt("PUT", SEARCH_ROUTE, {
        "expected_revision": profile["revision"], "provider_id": "tavily", "limits": limits,
    }, idempotency_key=str(uuid4())):
        st.rerun()


def _save_sources(attempt, revision, rows):
    if not isinstance(rows, list):
        raise ValueError("فهرست منابع باید یک آرایهٔ JSON باشد.")
    normalized = [SourceRule.model_validate(row).model_dump(mode="json") for row in rows]
    if attempt("PUT", SOURCES_ROUTE, {
        "expected_revision": revision, "sources": normalized,
    }, idempotency_key=str(uuid4())):
        st.rerun()


def source_settings(attempt):
    st.header("منابع پژوهش")
    snapshot = attempt("GET", SOURCES_ROUTE)
    if snapshot is None:
        return
    revision = snapshot["revision"]
    rows = snapshot["sources"]
    st.caption(f"نسخهٔ فهرست · {revision} | منابع فعال · {sum(bool(row['enabled']) for row in rows)}")
    st.caption("مسیرها را در هر سطر به صورت آرایهٔ JSON وارد کنید. نمونه: [\"/thieu1995/mealpy\"].")
    fields = ["source_id", "label", "hostname", "path_prefixes", "source_kind", "include_subdomains", "enabled"]
    display_rows = [
        {key: row[key] for key in fields} | {"path_prefixes": json.dumps(row["path_prefixes"], ensure_ascii=False)}
        for row in rows
    ]
    editor = pd.DataFrame(display_rows, columns=fields).astype({
        "source_id": "string", "label": "string", "hostname": "string",
        "path_prefixes": "string", "source_kind": "string", "include_subdomains": "boolean", "enabled": "boolean",
    })
    with st.form(f"source-editor:{revision}"):
        edited = st.data_editor(
            editor, num_rows="dynamic", hide_index=True, width="stretch",
            column_config={
                "source_id": st.column_config.TextColumn("شناسه", required=True),
                "label": st.column_config.TextColumn("عنوان", required=True),
                "hostname": st.column_config.TextColumn("دامنه", required=True),
                "path_prefixes": st.column_config.TextColumn("مسیرها", required=True),
                "source_kind": st.column_config.SelectboxColumn("نوع", options=SOURCE_KINDS, required=True),
                "include_subdomains": st.column_config.CheckboxColumn("زیردامنه‌ها", default=False),
                "enabled": st.column_config.CheckboxColumn("فعال", default=True),
            },
        )
        save = st.form_submit_button("ثبت نسخهٔ جدید منابع")
    if save:
        try:
            edited_rows = edited.fillna({"include_subdomains": False, "enabled": True}).to_dict("records")
            for row in edited_rows:
                raw_paths = row["path_prefixes"]
                if not isinstance(raw_paths, str):
                    raise ValueError("مسیرهای هر منبع را وارد کنید.")
                row["path_prefixes"] = strict_json_loads(raw_paths, limit=32 * 1024)
            _save_sources(attempt, revision, edited_rows)
        except ValueError as error:
            st.error(str(error).splitlines()[0])
    st.download_button(
        "خروجی JSON منابع", json.dumps(rows, indent=2, ensure_ascii=False).encode("utf-8"),
        "research-sources.json", "application/json",
    )
    with st.expander("ورود فهرست JSON"):
        st.caption("فهرست ورودی پس از ثبت، جایگزین فهرست فعلی می‌شود و یک نسخهٔ جدید می‌سازد.")
        upload = st.file_uploader("فایل JSON منابع", type=["json"], key=f"source-import:{revision}")
        if st.button("ثبت فهرست واردشده", disabled=upload is None, key=f"save-source-import:{revision}"):
            try:
                _save_sources(attempt, revision, strict_json_loads(upload.getvalue(), limit=256 * 1024))
            except ValueError as error:
                st.error(str(error).splitlines()[0])


def outcome_panel(outcome, *, details=False):
    if not outcome:
        st.info("اطلاعات پژوهش هنوز ثبت نشده است.")
        return
    state = outcome.get("status", "pending")
    st.write(RESEARCH_STATUS.get(state, state))
    documents = outcome.get("documents") or []
    passages = outcome.get("evidence") or []
    hits = outcome.get("hits") or []
    successful = sum(row.get("fetch_status") in {"read", "revalidated", "cached"} for row in documents)
    st.caption(
        f"تلاش جست‌وجو {outcome.get('search_attempts', 0)} · نتیجهٔ جست‌وجو {len(hits)} · "
        f"سند قابل استفاده {successful} · قطعهٔ شاهد {len(passages)}"
    )
    if outcome.get("cache_only"):
        st.info("پژوهش از شواهد ذخیره‌شده استفاده کرد. این حالت می‌تواند تماس با مدل زبانی داشته باشد.")
    if outcome.get("reason_codes"):
        st.text(" · ".join(outcome["reason_codes"]))
    for query in (outcome.get("query_plan") or {}).get("queries", []):
        st.text(f"{query['query_id']} · {query['text']}")
    if not details:
        return
    cost = outcome.get("cost")
    st.caption(f"کل درخواست‌های وب · {outcome.get('http_requests', 0)}")
    st.caption(f"هزینهٔ گزارش‌شدهٔ جست‌وجو · {cost if cost is not None else 'نامشخص'}")
    for document in documents:
        label = CONTENT_LABELS.get(document.get("content_level"), "دریافت‌نشده")
        with st.expander(f"{document['source_id']} · {label} · {document['fetch_status']}"):
            st.text(document.get("final_url") or document.get("requested_url", ""))
            st.text(f"تاریخ انتشار · {document.get('published_at') or 'نامشخص'}")
            st.text(f"دریافت · {document.get('fetched_at') or '—'}")
            st.text(f"آخرین اعتبارسنجی · {document.get('validated_at') or '—'}")
            if document.get("cache_age_hours") is not None:
                st.text(f"سن حافظه · {document['cache_age_hours']:.2f} ساعت")
            if document.get("truncated"):
                st.warning("استخراج این متن ناقص است.")
            if document.get("reason_code"):
                st.text(document["reason_code"])
            for row in passages:
                if row["document_id"] == document["document_id"]:
                    st.text(f"{row['evidence_id']} · {row['start']}–{row['end']}")
                    st.text(row["text"])
            st.json(document, expanded=False)
    if hits:
        with st.expander("نتایج جست‌وجو و علت حذف"):
            st.json(hits, expanded=False)


def research_report_panel(report):
    st.subheader("پژوهش و شواهد وب")
    requested = (report.get("policy") or {}).get("selection_policy")
    effective = report.get("effective_selection_policy") or requested
    if effective:
        st.text(f"سیاست درخواستی · {requested or '—'}\nسیاست مؤثر · {effective}")
    if report.get("research_error"):
        st.text(str(report["research_error"]))
    if requested == "web_research_then_pilot" and effective == "catalog_llm_then_pilot":
        st.warning("ادامه بدون شواهد وب کافی")
    outcome_panel(report.get("research"), details=True)
    if plan := report.get("advisor_plan_v2"):
        for candidate in plan.get("candidates", []):
            with st.expander(f"پیشنهاد اجرایی · {candidate['implementation_id']}"):
                st.text(candidate["rationale_fa"])
                st.text(candidate["config_id"])
                scope = "شاهد در سطح خانواده" if candidate.get("evidence_scope") == "family_level" else None
                if scope:
                    st.caption(scope)
                st.text(" · ".join(candidate.get("evidence_refs", [])))
        for index, finding in enumerate(plan.get("research_findings", []), start=1):
            with st.expander(f"یافتهٔ پژوهشی {index}"):
                st.text(finding["name"])
                if finding.get("variant"):
                    st.text(finding["variant"])
                st.text("دارای نگاشت اجرایی" if finding.get("implementation_match") else "بدون اجرای تأییدشده")
                st.text(finding["mapping_reason"])
                if finding.get("uncertainty"):
                    st.text(finding["uncertainty"])
                for claim in finding.get("claims", []):
                    st.text(f"{claim['status']} · {claim['text']}")
                    st.text(" · ".join(claim.get("evidence_refs", [])))
