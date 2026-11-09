"""Persian Streamlit client. All state mutations go through the local API."""

from __future__ import annotations

import json
import os

import httpx
import pandas as pd
import streamlit as st

from p_mc_optimizer.reporting import CLAIM_FA, EVALUATION_COLUMNS, csv_bytes, progress_figure
from p_mc_optimizer.ui.catalog import catalog_page
from p_mc_optimizer.ui.connections import connections_page, model_settings
from p_mc_optimizer.ui.executions import executions_page
from p_mc_optimizer.ui.research import research
from p_mc_optimizer.ui.theme import UI_CSS
from p_mc_optimizer.ui.web_research import (
    research_report_panel,
    source_settings,
)
from p_mc_optimizer.ui.wizard import reset_wizard, wizard
from p_mc_optimizer.ui.workspace import LIBRARY, PROBLEM, RUNS, SETTINGS, navigate, navigation


class ApiFailure(Exception):
    pass


def call(method, route, data=None, *, idempotency_key=None):
    headers = {"X-PMC-Internal-Token": os.environ["PMC_INTERNAL_TOKEN"]}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    args = {"json": data} if data is not None else {}
    try:
        with httpx.Client(base_url=os.environ["PMC_API_URL"], trust_env=False, timeout=315) as client:
            reply = client.request(method, route, headers=headers, **args)
    except httpx.HTTPError:
        raise ApiFailure("ارتباط با سرویس محلی برقرار نشد.") from None
    if reply.is_error:
        try:
            body = reply.json()
            error = body.get("detail", body)
            raise ApiFailure(f"{error.get('error_code', '')} · {error.get('message_fa', 'درخواست پذیرفته نشد.')}")
        except (ValueError, AttributeError):
            raise ApiFailure("سرویس محلی پاسخ معتبر نداد.") from None
    return reply.content if route.startswith("/artifacts/") else reply.json()


def attempt(method, route, data=None, **kwargs):
    try:
        return call(method, route, data, **kwargs)
    except ApiFailure as error:
        st.error(str(error))
        return None


SOLUTION_LABELS = {
    "pending": "در انتظار نتیجه", "valid_best_found": "جواب معتبر پیدا شد",
    "no_valid_solution": "جواب معتبر پیدا نشد", "validation_failed": "جواب تأیید نشد",
    "validation_incomplete": "بررسی جواب کامل نشد",
}


def report_panel(job_id, job):
    if job["job_status"] not in {"completed", "failed", "cancelled"}:
        st.info("گزارش پس از ثبت نتیجهٔ نهایی در دسترس خواهد بود.")
        return
    item = attempt("GET", f"/jobs/{job_id}/report")
    if not item:
        return
    report = item["report"]
    with st.container(key="result-summary"):
        st.subheader(SOLUTION_LABELS.get(report.get("solution_status"), "خلاصهٔ نتیجه"))
        objective = report.get("objective_raw")
        solver = report.get("selected_algorithm_id")
        value_col, method_col, check_col = st.columns(3)
        value_col.metric("مقدار تابع هدف", f"{objective:.15g}" if objective is not None else "—")
        method_col.metric("روش منتخب", solver.rsplit(":", 1)[-1] if solver else "—")
        verification = report.get("verification") or {}
        verified = verification.get("validation_status") or verification.get("status")
        check_col.metric("بازبینی جواب", {"passed": "تأیید شد", "failed": "تأیید نشد", "incomplete": "ناقص"}.get(verified, "—"))
        if report.get("best_x") is not None:
            st.code(f"x = {report['best_x']}", language=None)
        st.caption(CLAIM_FA)
    explanation = report.get("explanation") or {}
    if explanation:
        st.subheader("توضیح مدل" if report.get("explanation_status") == "generated" else "توضیح قالبی برنامه")
        for key in ("summary_fa", "selection_explanation_fa"):
            if line := explanation.get(key):
                st.text(line)
        for line in explanation.get("limitations_fa", []):
            st.text(line)
    if error := report.get("error"):
        message = error.get("message_fa") if isinstance(error, dict) else None
        st.error(message or "اجرا با خطا پایان یافت.")
    if report.get("schema_version") == "2.0":
        research_report_panel(report)
    if report.get("progress"):
        st.html(progress_figure(report["progress"]))
        st.caption("امتیاز کمینه‌سازی بر حسب تعداد فراخوانی آغازشدهٔ تابع هدف. برای بیشینه‌سازی، امتیاز برابر منفی تابع هدف است.")
    for key, label in [("decision", "شواهد انتخاب و رتبه‌بندی"), ("verification", "بررسی مجدد"),
                       ("diagnostics", "اطلاعات عیب‌یابی"), ("error", "خطا")]:
        if report.get(key) is not None:
            with st.expander(label):
                st.json(report[key])
    metrics = {metric["algorithm_id"]: metric for metric in (report.get("decision") or {}).get("metrics", [])}
    pilot_rows = []
    for trial in report.get("trials", []):
        if trial["phase"] == "pilot":
            metric = metrics.get(trial["algorithm_id"], {})
            pilot_rows.append({
                "روش": trial["algorithm_id"], "بذر": trial["seed"], "FE": trial["started_evaluations"],
                "هدف اصلی": trial["objective_raw"], "امتیاز کمینه‌سازی": trial["score_for_minimization"],
                "میانه": metric.get("median_score"), "IQR نرمال‌شده": metric.get("normalized_iqr"),
                "واجد شرایط": metric.get("eligible"), "علت": metric.get("reason") or trial.get("error_code"),
            })
    if pilot_rows:
        st.subheader("مقایسهٔ پایلوت‌ها")
        st.dataframe(pilot_rows, hide_index=True)
    if report.get("trials") or report.get("solution_origin"):
        with st.expander("جزئیات اجرای روش‌ها"):
            if report.get("solution_origin"):
                st.write("منشأ جواب")
                st.json(report["solution_origin"], expanded=False)
            if report.get("trials"):
                st.json(report["trials"], expanded=False)
    st.subheader("دریافت گزارش")
    names = {
        "report_json": "report.json", "report_html": "report.html", "trials_csv": "trials.csv",
        "research_json": "research.json", "sources_csv": "sources.csv",
    }
    for artifact in item["artifacts"]:
        if name := names.get(artifact["kind"]):
            data = attempt("GET", f"/artifacts/{artifact['artifact_id']}")
            if data:
                st.download_button(name, data, name)
    histories = report.get("execution_history_artifacts", [])
    if histories and st.button("آماده‌سازی تاریخچهٔ ارزیابی‌ها"):
        def records():
            for aid in histories:
                content = call("GET", f"/artifacts/{aid}")
                for line in content.splitlines():
                    row = json.loads(line)
                    if row.get("kind") == "result":
                        yield row["record"]
                    elif "evaluation_id" in row:
                        yield row
        try:
            st.download_button("evaluation_history.csv", csv_bytes(records(), EVALUATION_COLUMNS),
                               "evaluation_history.csv")
        except (ValueError, ApiFailure):
            st.error("تاریخچهٔ کامل برای دریافت در دسترس نیست.")


def open_problem(problem=None):
    reset_wizard(problem)
    navigate(PROBLEM)


def open_library():
    navigate(LIBRARY)


def open_connections(focus):
    st.session_state["connection-return"] = st.session_state.get("workspace-page", PROBLEM)
    st.session_state["connection-focus"] = focus
    navigate(SETTINGS)


def return_from_connections():
    navigate(st.session_state.get("connection-return", PROBLEM))


def open_execution(job_id):
    st.session_state.job_id = job_id
    st.session_state.pop("run-problem-summary", None)
    navigate(RUNS)
    st.rerun()


def library():
    st.title("منابع و الگوریتم‌ها")
    st.caption("منابع مجاز جست‌وجو و روش‌هایی را که این محیط می‌تواند اجرا کند بررسی کنید.")
    sources, catalog, experiments = st.tabs(
        ["منابع وب", "الگوریتم‌ها", "آزمایش پژوهشی"], key="library-tab", on_change="rerun",
    )
    if sources.open:
        with sources:
            source_settings(attempt)
    if catalog.open:
        with catalog:
            catalog_page(attempt)
    if experiments.open:
        with experiments:
            research(attempt, open_execution=open_execution)


def connections():
    connections_page(
        attempt,
        on_back=return_from_connections if st.session_state.get("connection-return") else None,
        focus=st.session_state.get("connection-focus"),
    )


def main():
    st.set_page_config(page_title="SolverScout | بهینه‌ساز محلی", layout="wide", initial_sidebar_state="expanded")
    st.html(UI_CSS)
    pages = {
        PROBLEM: lambda: wizard(attempt, open_execution, open_connections, open_library),
        RUNS: lambda: executions_page(attempt, open_problem, model_settings, report_panel),
        LIBRARY: library,
        SETTINGS: connections,
    }
    pages[navigation()]()


if __name__ == "__main__":
    main()
