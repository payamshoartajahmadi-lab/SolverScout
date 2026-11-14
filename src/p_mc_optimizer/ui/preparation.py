"""Explicit UI for the same bounded university preparation used by the CLI."""
from __future__ import annotations

from uuid import uuid4

import streamlit as st

from p_mc_optimizer.reporting.presentation import method_display_name

PREPARATION_ROUTE = "/demo/preparation"


def _preparation_status(value):
    if not value:
        return
    labels = {"not_started": "هنوز آماده‌سازی نشده است.", "running": "آماده‌سازی در حال انجام است…",
              "completed": "آماده‌سازی پایان یافت.", "blocked": "آماده‌سازی به اقدام شما نیاز دارد.",
              "failed": "آماده‌سازی کامل نشد."}
    if value.get("ready"):
        st.success("آماده‌سازی درخواستی کامل شد.")
    else:
        st.info(labels.get(value.get("status"), "وضعیت آماده‌سازی ثبت شد."))
    columns = st.columns(3)
    methods = value.get("methods") or []
    columns[0].metric("روش‌های آماده", sum(row.get("status") == "qualified" and
                                          row.get("pilot_500_compatible") is True and
                                          row.get("budget_policy_status") in {"qualified", "not_applicable"}
                                          for row in methods))
    columns[1].metric("مصرف آزمون‌های آماده‌سازی", f"{value.get('charged_evaluations', 0):,}")
    official = value.get("official_source") or {}
    columns[2].metric("اسناد منبع رسمی", official.get("usable_documents", 0))
    budget = value.get('qualification_budget')
    budget_label = f"{budget:,} ارزیابی" if type(budget) is int else "هنوز تعیین نشده"
    st.caption(f"سقف مستقل آماده‌سازی: {budget_label} · "
               f"رزرو فعال: {value.get('reserved_evaluations', 0):,} ارزیابی. "
               "این مصرف متعلق به آزمون‌های سازگاری است و مصرف مسئلهٔ شما نیست.")
    if methods:
        with st.expander("وضعیت شش روش"):
            for row in methods:
                prepared = (row.get("status") == "qualified" and row.get("pilot_500_compatible") is True
                            and row.get("budget_policy_status") in {"qualified", "not_applicable"})
                st.text(method_display_name(row.get("implementation_id")) + " · " +
                        ("آماده" if prepared else "نیازمند آماده‌سازی"))
    for error in value.get("errors") or []:
        st.warning(error.get("message_fa") or "یکی از مراحل آماده‌سازی کامل نشد؛ جزئیات را بررسی کنید.")


def preparation_panel(attempt):
    with st.expander("آماده‌سازی نسخهٔ دانشگاهی", expanded=False):
        st.caption("شش روش موجود را با آزمون محلی آماده کنید و مستندات رسمی را دریافت کنید. "
                   "گواهی معتبر دوباره آزموده نمی‌شود و این عملیات تکرارپذیر است.")
        official = st.checkbox("فعال‌سازی و آماده‌سازی منبع رسمی MEALPY", value=False,
                               key="university-enable-official-source")
        current = attempt("GET", PREPARATION_ROUTE)
        running = bool(current and current.get("status") == "running")
        if st.button("آماده‌سازی نسخهٔ دانشگاهی", type="primary", disabled=running,
                     key="university-prepare"):
            payload = {"enable_official_source": official, "qualification_budget": 100000}
            pending = st.session_state.get("university-preparation-request")
            if not pending or pending["payload"] != payload:
                pending = {"payload": payload, "key": str(uuid4())}
                st.session_state["university-preparation-request"] = pending
            result = attempt("POST", PREPARATION_ROUTE, payload, idempotency_key=pending["key"])
            if result:
                st.session_state.pop("university-preparation-request", None)
                st.rerun()
        if running:
            @st.fragment(run_every=2)
            def monitor():
                value = attempt("GET", PREPARATION_ROUTE)
                _preparation_status(value)
                if value and value.get("status") != "running":
                    if st.button("بازخوانی آمادگی"):
                        st.rerun(scope="app")
            monitor()
        else:
            _preparation_status(current)
