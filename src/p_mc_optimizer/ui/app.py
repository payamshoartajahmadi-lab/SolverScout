"""Persian Streamlit client. All state mutations go through the local API."""

from __future__ import annotations

import json
import os
from datetime import datetime
from uuid import uuid4

import httpx
import pandas as pd
import streamlit as st

from p_mc_optimizer.domain.models import ProblemInput, strict_json_loads
from p_mc_optimizer.reporting import CLAIM_FA, EVALUATION_COLUMNS, csv_bytes, progress_figure
from p_mc_optimizer.ui.catalog import catalog_page
from p_mc_optimizer.ui.connections import connections_page, model_settings
from p_mc_optimizer.ui.research import research
from p_mc_optimizer.ui.theme import UI_CSS
from p_mc_optimizer.ui.web_research import (
    SEARCH_BLOCKERS,
    outcome_panel,
    research_report_panel,
    search_settings,
    source_settings,
)
from p_mc_optimizer.ui.wizard import METHOD_LABELS, reset_wizard, wizard
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


JOB_STATES = {
    "queued": "در صف اجرا", "running": "در حال اجرا", "needs_input": "نیازمند تکمیل مسئله",
    "needs_configuration": "نیازمند تنظیمات", "completed": "اجرا کامل شد",
    "failed": "اجرا ناموفق بود", "cancelled": "اجرا لغو شد",
}
PHASE_LABELS = {
    "intake": "دریافت مسئله", "formulating": "صورت‌بندی مسئله", "validating_input": "بررسی ورودی",
    "preflight": "پیش‌آزمون", "profiling": "نمونه‌برداری اولیه", "planning": "آماده‌سازی روش‌ها",
    "research_planning": "آماده‌سازی جست‌وجو", "searching": "جست‌وجوی منابع",
    "fetching_evidence": "دریافت شواهد", "piloting": "آزمون روش‌ها",
    "selecting": "انتخاب روش", "solving": "حل مسئله", "verifying": "بررسی جواب", "reporting": "آماده‌سازی گزارش",
}
SOLUTION_LABELS = {
    "pending": "در انتظار نتیجه", "valid_best_found": "جواب معتبر پیدا شد",
    "no_valid_solution": "جواب معتبر پیدا نشد", "validation_failed": "جواب تأیید نشد",
    "validation_incomplete": "بررسی جواب کامل نشد",
}
TERMINAL_STATES = {"completed", "failed", "cancelled"}

def model_panel(job_id, job):
    item = attempt("GET", f"/jobs/{job_id}/problem")
    if not item:
        return
    if item["frozen"]:
        revision = item["revision"]
        source = revision["spec"]["objective"]["source"]
        st.code(source.get("expression") or source.get("function_id"), language=None)
        st.dataframe(revision["spec"]["variables"], hide_index=True)
        with st.expander("جزئیات مدل و منشأ فیلدها"):
            st.json(revision)
    elif item.get("draft"):
        draft = item["draft"]
        st.json(draft["extracted_fields"])
        if draft.get("missing_fields"):
            st.write("فیلدهای لازم", draft["missing_fields"])
        if job["job_status"] != "needs_input":
            return
        versions = {"expected_job_version": job["version"], "expected_draft_version": draft["draft_version"]}
        questions = draft.get("ambiguities") or []
        if questions:
            with st.form("answer-text"):
                answers = [{"question_id": q["question_id"], "answer": st.text_input(
                    q["question_fa"], key=f"q-{q['question_id']}")} for q in questions]
                send = st.form_submit_button("ارسال پاسخ متنی")
            if send and attempt("POST", f"/jobs/{job_id}/answers", {
                **versions, "mode": "textual_answers", "answers": [a for a in answers if a["answer"].strip()],
            }):
                st.rerun()
        with st.form("complete"):
            completed = st.text_area("مدل کامل به‌صورت JSON", height=250,
                                     help="مدل صریح با قرارداد ProblemInput بررسی می‌شود.")
            send = st.form_submit_button("ثبت مدل کامل")
        if send:
            try:
                problem = ProblemInput.model_validate(strict_json_loads(completed, limit=256 * 1024))
            except ValueError as exc:
                st.error(str(exc).splitlines()[0])
            else:
                if attempt("POST", f"/jobs/{job_id}/answers", {
                    **versions, "mode": "structured_completion", "problem": problem.model_dump(mode="json"),
                }):
                    st.rerun()
    else:
        st.info("مدل هنوز آماده نشده است.")


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


def execution():
    st.title("اجرای حل")
    job_id = st.session_state.get("job_id")
    if not job_id:
        st.info("برای دیدن نتیجه، یک مسئلهٔ جدید اجرا کنید یا از تاریخچه یک اجرا را باز کنید.")
        new_col, history_col = st.columns(2)
        new_col.button("مسئلهٔ جدید", type="primary", on_click=navigate, args=(PROBLEM,))
        history_col.button("دیدن تاریخچه", on_click=navigate, args=(RUNS,))
        return
    initial = attempt("GET", f"/jobs/{job_id}")
    if initial is None:
        return
    active = initial["job_status"] in {"queued", "running"}

    @st.fragment(run_every=1 if active else None)
    def monitor():
        job = attempt("GET", f"/jobs/{job_id}")
        if job is None:
            return
        now_active = job["job_status"] in {"queued", "running"}
        if active != now_active:
            st.rerun()
        state = job["job_status"]
        terminal = state in TERMINAL_STATES
        with st.container(key="run-overview"):
            label = JOB_STATES.get(state, "وضعیت اجرا")
            if state == "completed":
                st.success(label)
            elif state == "failed":
                st.error(label)
            elif state in {"needs_input", "needs_configuration", "cancelled"}:
                st.warning(label)
            else:
                st.subheader(label)
            if not terminal:
                st.caption(PHASE_LABELS.get(job["phase"], "در حال پردازش"))
            used = job["budget"].get("known_started", 0)
            total = job["policy"]["max_objective_evaluations"]
            st.progress(min(1.0, used / total), text=f"{used:,} از {total:,} ارزیابی تابع هدف")
        if job.get("blocked_reason"):
            st.warning(job["blocked_reason"])
        if job.get("cancel_requested"):
            st.warning("درخواست لغو ثبت شد. وضعیت پس از توقف عددی به‌روزرسانی می‌شود.")
        elif job["job_status"] in {"queued", "running", "needs_input", "needs_configuration"}:
            if st.button("لغو اجرا", key="cancel-run"):
                if attempt("POST", f"/jobs/{job_id}/cancel", {"expected_job_version": job["version"]}):
                    st.rerun()
        if job["policy"].get("policy_version") == "2.0":
            with st.expander("وضعیت پژوهش", expanded=job["phase"] in {
                "research_planning", "searching", "fetching_evidence",
            }):
                result = attempt("GET", f"/jobs/{job_id}/research")
                outcome_panel((result or {}).get("outcome", result))
        with st.expander("رویدادها و اجرای الگوریتم‌ها"):
            st.code(job_id, language=None)
            key = f"events:{job_id}"
            events = st.session_state.get(key, [])
            cursor = events[-1]["sequence"] if events else 0
            fresh = attempt("GET", f"/jobs/{job_id}/events?after_sequence={cursor}&limit=100")
            if isinstance(fresh, list):
                events = [*events, *fresh][-100:]
                st.session_state[key] = events
            st.json(events, expanded=False)
            trials = attempt("GET", f"/jobs/{job_id}/trials")
            if trials:
                st.json(trials, expanded=False)
        model_tab, report_tab = st.tabs(
            ["مدل مسئله", "نتایج"], default="نتایج" if terminal else "مدل مسئله",
            key=f"job-view:{job_id}:{terminal}", on_change="rerun",
        )
        if model_tab.open:
            with model_tab:
                model_panel(job_id, job)
        if report_tab.open:
            with report_tab:
                report_panel(job_id, job)
        if job["job_status"] == "needs_configuration":
            reason = job.get("blocked_reason") or ""
            error = job.get("error") or {}
            code = (error.get("error_code") or error.get("code") or reason) if isinstance(error, dict) else reason
            if code in SEARCH_BLOCKERS:
                _repair_search(job_id, job, code)
            else:
                model_settings(attempt, job_id, job["version"])

    monitor()


def _repair_search(job_id, job, code):
    if code == "RESEARCH_EVIDENCE_INSUFFICIENT":
        st.info("منابع یا شواهد ذخیره‌شده را بررسی کنید و با تنظیمات اصلاح‌شده اجرای تازه‌ای بسازید.")
        return
    ready = search_settings(attempt, repair=True)
    if code == "SEARCH_RATE_LIMITED":
        st.info("پس از گذشت زمان اعلام‌شدهٔ سرویس، ادامهٔ همین اجرا را درخواست کنید.")
    else:
        st.info("برای ادامه، کلید همان تنظیمات جست‌وجو را ثبت کنید. اگر منابع یا پروفایل را تغییر داده‌اید، اجرای تازه بسازید.")
    if st.button("ادامهٔ پژوهش همین اجرا", disabled=not ready and code != "SEARCH_RATE_LIMITED"):
        if attempt("POST", f"/jobs/{job_id}/resume", {"expected_job_version": job["version"]}):
            st.rerun()


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
    navigate(RUNS)
    st.rerun()


def history():
    st.title("تاریخچه")
    st.caption("اجرای قبلی را انتخاب کنید تا مدل مسئله و نتیجهٔ آن باز شود.")
    jobs = attempt("GET", "/jobs")
    if jobs is None:
        return
    if not jobs:
        st.info("هنوز اجرایی ثبت نشده است.")
        st.button("مسئلهٔ جدید", type="primary", on_click=navigate, args=(PROBLEM,))
    for job in jobs:
        state_col, action_col = st.columns([3, 1], vertical_alignment="center")
        state_col.write(JOB_STATES.get(job["job_status"], "وضعیت اجرا"))
        method = METHOD_LABELS.get(job.get("policy", {}).get("selection_policy"), "اجرای بهینه‌سازی")
        created_at = job.get("created_at")
        when = datetime.fromisoformat(created_at).astimezone().strftime("%Y/%m/%d · %H:%M") if created_at else ""
        state_col.caption(f"{method} · {when}" if when else method)
        if action_col.button("باز کردن اجرا", key=job["job_id"]):
            open_execution(job["job_id"])
        st.divider()


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
        RUNS: execution if st.session_state.get("job_id") else history,
        LIBRARY: library,
        SETTINGS: connections,
    }
    pages[navigation()]()


if __name__ == "__main__":
    main()
