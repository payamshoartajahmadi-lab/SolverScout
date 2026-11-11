"""Browse the installed catalog and explicitly qualify solver configurations."""

from __future__ import annotations

from math import ceil
from urllib.parse import urlencode
from uuid import uuid4

import streamlit as st

CATALOG_ROUTE = "/catalog/implementations"
STATUSES = {
    "discovered": "کشف‌شده",
    "needs_configuration": "نیازمند پیکربندی",
    "qualified": "تأییدشده",
    "rejected": "ردشده",
    "disabled": "غیرفعال",
}
ACTIVE_BATCH = {"queued", "running"}


def _catalog_counts(result):
    columns = st.columns(3)
    columns[0].metric("کشف‌شده", result.get("discovered_count", 0))
    columns[1].metric("تأییدشده", result.get("qualified_count", 0))
    columns[2].metric("غیرفعال", result.get("counts", {}).get("disabled", 0))


def _qualification_monitor(attempt, batch_id):
    @st.fragment(run_every=2)
    def monitor():
        batch = attempt("GET", f"/catalog/qualifications/{batch_id}")
        if not batch:
            return
        st.text(f"{batch_id} · {batch['status']}")
        used = batch.get("started_evaluations", 0)
        limit = batch.get("max_evaluations", 0)
        st.caption(
            f"FE آزمون سازگاری · {used:,} / {limit:,} | "
            f"مصرف نامعلوم · {batch.get('charged_unknown', 0):,}"
        )
        if batch.get("cancel_requested"):
            st.info("درخواست لغو ثبت شد.")
        elif batch["status"] in ACTIVE_BATCH:
            if st.button("لغو آزمون سازگاری", key=f"cancel-qualification:{batch_id}"):
                attempt("POST", f"/catalog/qualifications/{batch_id}/cancel", idempotency_key=str(uuid4()))
                st.rerun(scope="app")
        for report in batch.get("reports", []):
            with st.expander(f"{report.get('config_id', report.get('base_config_id'))} · {report['status']}"):
                st.json(report, expanded=False)
        if batch["status"] not in ACTIVE_BATCH and st.button("بازخوانی وضعیت کاتالوگ"):
            st.rerun(scope="app")

    monitor()


def _qualification_actions(attempt, result):
    scope = st.selectbox("نوع تأیید", ["base", "budget_horizon.v1"], format_func=lambda value: {
        "base": "تأیید سازگاری پایه", "budget_horizon.v1": "آزمون تنظیم اجرا با بودجه",
    }[value])
    adapted = scope == "budget_horizon.v1"
    options = {
        row["config_id"]: row
        for row in result.get("suggested_configurations", [])
        if row.get("config_id") and (
            row.get("status") == "qualified" and row.get("budget_policy_status") == "NUMERIC_POLICY_QUALIFICATION_REQUIRED"
            if adapted else row.get("status") not in {"qualified", "disabled"}
        )
    }
    st.subheader("آزمون سازگاری پیکربندی‌ها")
    st.caption("آزمون‌ها با مسائل نمونه و در فرایند عددی جدا اجرا می‌شوند. مصرف آن‌ها مستقل از اجراهای حل است.")
    if options:
        selected = st.multiselect(
            "پیکربندی‌ها", list(options), key=f"qualification-configs:{scope}:{result.get('catalog_hash')}",
            format_func=lambda config_id: options[config_id]["class_name"],
        )
        seconds = 300 if adapted else st.number_input("سقف زمان آزمون (ثانیه)", 1, 120, 120)
        quota = result.get("budget_policy_fe_per_config" if adapted else "qualification_fe_per_config",
                           31996 if adapted else 3000) * len(selected)
        st.caption(f"{len(selected)} پیکربندی · حداکثر {quota:,} FE و {seconds} ثانیه")
        if st.button("شروع آزمون سازگاری", type="primary", disabled=not selected):
            data = {"config_ids": selected, "max_seconds": seconds, "scope": scope}
            pending = st.session_state.get("pending_qualification")
            if not pending or pending["payload"] != data:
                pending = {"payload": data, "key": str(uuid4())}
                st.session_state.pending_qualification = pending
            batch = attempt("POST", "/catalog/qualifications", data, idempotency_key=pending["key"])
            if batch:
                st.session_state.qualification_id = batch["qualification_id"]
                st.session_state.pop("pending_qualification", None)
                st.rerun()
    else:
        st.info("پیکربندی پیشنهادی در انتظار آزمون وجود ندارد.")
    if batch_id := st.session_state.get("qualification_id"):
        _qualification_monitor(attempt, batch_id)


def catalog_page(attempt):
    st.header("الگوریتم‌ها")
    st.caption("کلاس‌های کتابخانهٔ نصب‌شده و وضعیت پیکربندی‌هایی که این محیط می‌تواند اجرا کند.")
    if st.button("بازخوانی کتابخانهٔ نصب‌شده"):
        if attempt("POST", "/catalog/discover", idempotency_key=str(uuid4())) is not None:
            st.rerun()
    result = attempt("GET", f"{CATALOG_ROUTE}?limit=1")
    if result is None:
        return
    _catalog_counts(result)
    if result.get("library_version"):
        st.caption(f"MEALPY · {result['library_version']}")
    columns = st.columns(3)
    query = columns[0].text_input("جست‌وجوی نام یا شناسه", max_chars=150).strip()
    family = columns[1].selectbox("خانواده", ["", *result.get("family_ids", [])],
                                 format_func=lambda value: value or "همه")
    status = columns[2].selectbox("وضعیت", ["", *STATUSES],
                                 format_func=lambda value: STATUSES.get(value, "همه"))
    compatible = st.checkbox("فقط سازگار با پایلوت ۵۰۰ FE")
    filters = {key: value for key, value in {
        "query": query, "family": family, "status": status,
        "pilot_500_compatible": "true" if compatible else "",
    }.items() if value}
    size = 25
    params = {"limit": size, **filters}
    page = attempt("GET", f"{CATALOG_ROUTE}?{urlencode(params)}") if filters else None
    if filters and page is None:
        return
    total = (page or result).get("total", 0)
    if total > size:
        index = st.number_input("صفحه", 1, ceil(total / size), key=f"catalog-page:{query}:{family}:{status}:{compatible}")
        params["offset"] = (index - 1) * size
        if index > 1:
            page = None
    page = page or attempt("GET", f"{CATALOG_ROUTE}?{urlencode(params)}")
    if page and page.get("items"):
        for item in page["items"]:
            name = item["class_name"]
            state = STATUSES.get(item["status"], item["status"])
            with st.expander(f"{name} · {state}"):
                st.text(item["implementation_id"])
                st.caption(f"خانواده {item['family_id']} · MEALPY {item['library_version']}")
                if item.get("reason"):
                    st.text(item["reason"])
                if item.get("config_id"):
                    st.text(f"پیکربندی · {item['config_id']}")
                if item.get("configuration"):
                    st.json(item["configuration"], expanded=False)
                if report := item.get("qualification"):
                    st.caption(
                        "سازگار با پایلوت ۵۰۰ FE" if report.get("pilot_500_compatible")
                        else "سازگاری پایلوت ۵۰۰ FE ثبت نشده است."
                    )
                    st.json(report, expanded=False)
                if item.get("budget_policy_status") not in (None, "not_applicable"):
                    st.caption("تنظیم اجرا با بودجه تأیید شده است." if item["budget_policy_status"] == "qualified"
                               else "تنظیم اجرا با بودجه به آزمون جداگانه نیاز دارد.")
                    if item.get("budget_policy_qualification"):
                        st.json(item["budget_policy_qualification"], expanded=False)
    elif page is not None:
        st.info("الگوریتمی با این فیلترها پیدا نشد.")
    _qualification_actions(attempt, result)
