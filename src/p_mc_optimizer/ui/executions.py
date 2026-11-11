"""Execution history, live progress and recovery for the local job API."""
from __future__ import annotations

import re
from datetime import datetime

import streamlit as st

from p_mc_optimizer.domain.models import ProblemInput, strict_json_loads
from p_mc_optimizer.ui.library import sync_run_panel
from p_mc_optimizer.ui.web_research import SEARCH_BLOCKERS, outcome_panel, search_settings
from p_mc_optimizer.ui.workspace import LIBRARY, navigate


ACTIVE_STATES = {"queued", "running"}
TERMINAL_STATES = {"completed", "failed", "cancelled"}
JOB_LABELS = {
    "queued": "در صف اجرا", "running": "در حال اجرا", "needs_input": "نیازمند پاسخ شما",
    "needs_configuration": "نیازمند تنظیمات", "completed": "اجرا کامل شد",
    "failed": "اجرا ناموفق بود", "cancelled": "اجرا لغو شد",
}
POLICY_LABELS = {
    "web_research_then_pilot": "پژوهش وب و مقایسهٔ روش‌ها",
    "evidence_research_then_pilot": "پژوهش در منابع و مقایسهٔ روش‌ها",
    "catalog_llm_then_pilot": "پیشنهاد از کتابخانهٔ موجود",
    "catalog_fixed": "روش انتخابی شما",
    "fixed_solver": "روش ثابت",
    "llm_shortlist_then_pilot": "مشاور مدل و مقایسهٔ روش‌ها",
    "llm_only": "پیشنهاد مدل زبانی",
    "random_solver": "روش تصادفی",
    "rule_based": "روش انتخاب‌شده با قاعدهٔ ثابت",
    "pilot_only": "مقایسهٔ چهار روش",
}
PHASES = {
    "intake": ("input", "در حال دریافت مسئله"),
    "formulating": ("input", "در حال صورت‌بندی مسئله"),
    "validating_input": ("input", "در حال بررسی ورودی مسئله"),
    "preflight": ("input", "در حال بررسی تابع هدف"),
    "profiling": ("input", "در حال نمونه‌برداری اولیه"),
    "research_planning": ("research", "در حال آماده‌سازی جست‌وجو"),
    "refreshing_sources": ("research", "در حال به‌روزرسانی منابع"),
    "searching": ("research", "در حال جست‌وجوی منابع مجاز"),
    "fetching_evidence": ("research", "در حال خواندن منابع مجاز"),
    "planning": ("selection", "در حال آماده‌سازی روش‌های مناسب"),
    "piloting": ("selection", "در حال مقایسهٔ روش‌ها"),
    "selecting": ("selection", "در حال انتخاب روش حل"),
    "solving": ("solve", "در حال حل مسئله"),
    "verifying": ("verify", "در حال بررسی دوبارهٔ جواب"),
    "reporting": ("verify", "در حال آماده‌سازی گزارش"),
}
STAGE_LABELS = {
    "input": "بررسی مسئله", "research": "پژوهش", "selection": "انتخاب روش",
    "solve": "حل", "verify": "بررسی جواب",
}
BLOCK_LABELS = {
    "SEARCH_CONFIGURATION_REQUIRED": "اتصال جست‌وجوی وب آماده نیست.",
    "SEARCH_AUTH_FAILED": "Tavily کلید جست‌وجو را نپذیرفت.",
    "SEARCH_RATE_LIMITED": "سرویس جست‌وجو موقتاً درخواست تازه نمی‌پذیرد.",
    "RESEARCH_EVIDENCE_INSUFFICIENT": "شواهد کافی برای ادامهٔ پژوهش پیدا نشد.",
    "LIBRARY_EMPTY": "کتابخانه برای منابع این اجرا خالی است.",
    "LIBRARY_EVIDENCE_STALE": "شواهد موجود از سقف تازگی این اجرا قدیمی‌ترند.",
    "LOCAL_INDEX_UNAVAILABLE": "فهرست جست‌وجوی محلی در دسترس نیست.",
    "SOURCE_SYNC_IN_PROGRESS": "به‌روزرسانی یکی از منابع هنوز در جریان است.",
    "SOURCE_CONFIG_CHANGED": "تنظیمات منبع پس از ثبت این اجرا تغییر کرده است.",
}
LIBRARY_BLOCKERS = {
    "LIBRARY_EMPTY", "LIBRARY_EVIDENCE_STALE", "LOCAL_INDEX_UNAVAILABLE",
    "SOURCE_SYNC_IN_PROGRESS", "SOURCE_CONFIG_CHANGED",
}
HISTORY_PAGE_SIZE = 50
_BOUND_NUMBER = r"[+\-−]?(?:\d+(?:[.٫]\d*)?|[.٫]\d+)(?:[eE][+\-−]?\d+)?"
_COMPARISON = r"\s*(?:<=|>=|≤|≥|<|>)\s*"
_BOUND_CHAIN = re.compile(
    rf"(?<![\w.]){_BOUND_NUMBER}{_COMPARISON}[A-Za-z_]\w*{_COMPARISON}{_BOUND_NUMBER}(?![\w.])"
)


def _count(value):
    return f"{value:,}".translate(str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹٬"))


def _research_job(job):
    return job.get("policy", {}).get("selection_policy") in {
        "web_research_then_pilot", "evidence_research_then_pilot",
    }


def _stage_tracker(job, current):
    stages = ["input", "selection", "solve", "verify"]
    if _research_job(job):
        stages.insert(1, "research")
    current_index = stages.index(current) if current in stages else 0
    items = []
    for index, stage in enumerate(stages):
        label = STAGE_LABELS[stage]
        if stage == "selection" and job.get("policy", {}).get("selection_policy") in {
            "web_research_then_pilot", "evidence_research_then_pilot", "catalog_llm_then_pilot",
            "llm_shortlist_then_pilot", "pilot_only",
        }:
            label = "مقایسهٔ روش‌ها"
        state = "done" if index < current_index else "current" if index == current_index else "pending"
        aria_current = ' aria-current="step"' if state == "current" else ""
        state_label = {"done": "انجام شد", "current": "مرحلهٔ فعلی", "pending": "در انتظار"}[state]
        items.append(
            f'<li data-state="{state}" aria-label="{label}، {state_label}"{aria_current}>'
            f'<span class="run-stage-marker" aria-hidden="true">{_count(index + 1)}</span>'
            f'<span class="run-stage-label">{label}</span></li>'
        )
    with st.container(key="run-stages"):
        st.html('<ol class="run-stage-list" aria-label="مراحل اجرا">' + "".join(items) + "</ol>")


def _budget(job):
    budget = job.get("budget") or {}
    total = budget.get("max_objective_evaluations") or job.get("policy", {}).get("max_objective_evaluations")
    if not total:
        return
    started = budget.get("known_started", 0)
    unknown = budget.get("charged_unknown", 0)
    with st.container(key="run-budget"):
        st.subheader("مصرف ارزیابی تابع هدف")
        st.progress(min(1.0, (started + unknown) / total), text=f"{_count(started + unknown)} از {_count(total)}")
        if unknown:
            st.caption(f"{_count(unknown)} ارزیابی با وضعیت نامشخص در حساب بودجه منظور شده است.")
        st.caption("این نوار مصرف بودجه را نشان می‌دهد. مرحلهٔ اجرا در بالای صفحه مشخص است.")


def _research_progress(attempt, job_id):
    result = attempt("GET", f"/jobs/{job_id}/research")
    if not result:
        return
    outcome = result.get("outcome") or result
    if outcome.get("cache_only"):
        st.caption("این اجرا از شواهد ذخیره‌شده استفاده می‌کند.")
    counts = []
    if "documents" in outcome:
        successful = sum(doc.get("fetch_status") in {"read", "revalidated", "cached"}
                         for doc in outcome["documents"])
        counts.append(f"{_count(successful)} سند قابل استفاده")
    if "search_attempts" in outcome:
        counts.append(f"{_count(outcome['search_attempts'])} تلاش جست‌وجو")
    if counts:
        st.write(" · ".join(counts))
    with st.expander("جزئیات پژوهش"):
        outcome_panel(outcome)


def _repair_search(attempt, job_id, job, code):
    if code == "RESEARCH_EVIDENCE_INSUFFICIENT":
        st.info("منابع یا شواهد ذخیره‌شده را بررسی کنید و پس از اصلاح تنظیمات، اجرای تازه‌ای بسازید.")
        return
    ready = search_settings(attempt, repair=True)
    if code == "SEARCH_RATE_LIMITED":
        st.info("پس از پایان مهلت اعلام‌شدهٔ سرویس، ادامهٔ همین اجرا را درخواست کنید.")
    else:
        st.caption("برای ادامه، کلید همان تنظیمات جست‌وجو را ثبت کنید. تغییر منابع یا تنظیمات به اجرای تازه نیاز دارد.")
    if st.button("ادامهٔ پژوهش همین اجرا", type="primary", disabled=not ready and code != "SEARCH_RATE_LIMITED"):
        if attempt("POST", f"/jobs/{job_id}/resume", {"expected_job_version": job["version"]}):
            st.rerun()


def _repair_library(attempt, job, code):
    st.warning(BLOCK_LABELS[code])
    st.button("باز کردن کتابخانهٔ منابع", on_click=navigate, args=(LIBRARY,))
    if code == "SOURCE_CONFIG_CHANGED":
        st.info("برای استفاده از تنظیمات تازه، اجرای جدیدی بسازید.")
        return
    if code == "LOCAL_INDEX_UNAVAILABLE":
        st.info("برای بررسی فهرست جست‌وجو، فرمان بررسی سلامت نصب را اجرا کنید.")
    if st.button("ادامهٔ همین اجرا پس از رفع مشکل", type="primary"):
        if attempt("POST", f"/jobs/{job['job_id']}/resume", {"expected_job_version": job["version"]}):
            st.rerun()


def _problem_model(attempt, job_id, job):
    item = attempt("GET", f"/jobs/{job_id}/problem")
    if not item:
        return
    if item.get("frozen"):
        revision = item["revision"]
        source = revision["spec"]["objective"]["source"]
        st.code(source.get("expression") or source.get("function_id"), language=None)
        st.dataframe(revision["spec"]["variables"], hide_index=True)
        with st.expander("جزئیات مدل و منشأ فیلدها"):
            st.json(revision, expanded=False)
        return
    draft = item.get("draft")
    if not draft:
        st.info("مدل مسئله هنوز آماده نشده است.")
        return
    if job["job_status"] == "needs_input":
        _correction_form(attempt, job_id, job, draft)
    with st.expander("جزئیات پیش‌نویس مسئله"):
        st.json(draft["extracted_fields"], expanded=False)
        if draft.get("missing_fields"):
            st.write("فیلدهای لازم")
            st.json(draft["missing_fields"], expanded=False)


def _correction_form(attempt, job_id, job, draft):
    versions = {"expected_job_version": job["version"], "expected_draft_version": draft["draft_version"]}
    route = f"/jobs/{job_id}/answers"
    questions = draft.get("ambiguities") or []
    if questions:
        st.subheader("برای ادامه به این پرسش‌ها پاسخ دهید")
        if any(question["reason"] == "contradictory" for question in questions):
            st.caption("بازهٔ درست را برای هر متغیر بنویسید و هر دو کران آن را در پاسخ بیاورید.")
        else:
            st.caption("می‌توانید هر پرسشی را که پاسخ آن را می‌دانید تکمیل کنید.")
        with st.container(key="clarification-answers"), st.form(f"answer-text:{job_id}:{draft['draft_version']}"):
            answers = []
            for index, question in enumerate(questions, start=1):
                label = f"پاسخ پرسش {_count(index)}"
                path = re.fullmatch(r"/variables/(\d+)/(lower|upper)", question.get("field_path", ""))
                variables = (draft.get("extracted_fields") or {}).get("variables", [])
                if path and int(path[1]) < len(variables):
                    name = variables[int(path[1])].get("name")
                    if name:
                        label = f"کران {'پایین' if path[2] == 'lower' else 'بالای'} {name}"
                # Keep inequality signs and negative bounds in reading order inside Persian text.
                st.text(label if path else _BOUND_CHAIN.sub(lambda match: f"\u2066{match[0]}\u2069", question["question_fa"]))
                answer = st.text_input(label, max_chars=16000,
                                       key=f"answer:{job_id}:{draft['draft_version']}:{question['question_id']}")
                if path:
                    with st.expander("جزئیات پرسش"):
                        st.text(question["field_path"])
                        st.text(question["question_fa"])
                if answer.strip():
                    answers.append({"question_id": question["question_id"], "answer": answer.strip()})
            send = st.form_submit_button("ثبت پاسخ و ادامه", type="primary")
        if send:
            if not answers:
                st.warning("پاسخ دست‌کم یک پرسش را وارد کنید.")
            elif attempt("POST", route, {**versions, "mode": "textual_answers", "answers": answers}):
                st.rerun()
    else:
        st.info("برای ادامه، مدل کامل مسئله را در بخش تکمیل پیشرفته ثبت کنید.")
    with st.expander("تکمیل پیشرفته با JSON", expanded=not questions):
        with st.form(f"complete-problem:{job_id}:{draft['draft_version']}"):
            completed = st.text_area("مدل کامل مسئله", height=220)
            send = st.form_submit_button("ثبت مدل کامل و ادامه", type="secondary" if questions else "primary")
        if send:
            try:
                problem = ProblemInput.model_validate(strict_json_loads(completed, limit=256 * 1024))
            except ValueError as exc:
                st.error(str(exc).splitlines()[0])
            else:
                if attempt("POST", route, {**versions, "mode": "structured_completion",
                                            "problem": problem.model_dump(mode="json")}):
                    st.rerun()


def _technical_details(attempt, job_id, job):
    details = st.expander("جزئیات فنی و رویدادها", key=f"run-details:{job_id}", on_change="rerun")
    if not details.open:
        return
    with details:
        st.code(job_id, language=None)
        key = f"events:{job_id}"
        events = st.session_state.get(key, [])
        cursor = events[-1]["sequence"] if events else 0
        fresh = attempt("GET", f"/jobs/{job_id}/events?after_sequence={cursor}&limit=100")
        if isinstance(fresh, list):
            events = [*events, *fresh][-100:]
            st.session_state[key] = events
        st.json(events, expanded=False)
        if trials := attempt("GET", f"/jobs/{job_id}/trials"):
            st.json(trials, expanded=False)
        if error := job.get("error"):
            st.json(error, expanded=False)
        if job.get("job_status") != "needs_input":
            _problem_model(attempt, job_id, job)


def _monitor(attempt, job, model_repair):
    job_id = job["job_id"]
    state = job["job_status"]
    stage, phase_label = PHASES.get(job.get("phase"), ("input", "در حال پردازش"))
    title = phase_label if state == "running" else JOB_LABELS.get(state, "وضعیت اجرا")
    st.title(title)
    if summary := job.get("problem_summary") or st.session_state.get("run-problem-summary"):
        st.text(summary)
    else:
        st.caption(POLICY_LABELS.get(job.get("policy", {}).get("selection_policy"), "اجرای بهینه‌سازی"))
    _stage_tracker(job, stage)
    with st.container(key="run-status"):
        if job.get("cancel_requested"):
            st.warning("درخواست لغو ثبت شد. وضعیت پس از توقف عددی به‌روزرسانی می‌شود.")
        elif state == "running" and stage == "research":
            st.caption("شواهد برای پیشنهاد روش‌های قابل اجرا جمع‌آوری می‌شوند.")
            if job.get("phase") == "refreshing_sources" and (run_id := job.get("source_sync_run_id")):
                sync_run_panel(attempt, run_id)
            _research_progress(attempt, job_id)
        elif state in ACTIVE_STATES:
            st.caption("پس از پایان اجرا، نتیجه در همین صفحه نمایش داده می‌شود.")
        elif state == "needs_input":
            _problem_model(attempt, job_id, job)
        elif state == "needs_configuration":
            error = job.get("error") or {}
            reason = job.get("blocked_reason")
            code = (error.get("error_code") or error.get("code") or reason) if isinstance(error, dict) else reason
            if code in SEARCH_BLOCKERS:
                st.warning(BLOCK_LABELS.get(code, "برای ادامه، اتصال جست‌وجوی وب را بررسی کنید."))
                _repair_search(attempt, job_id, job, code)
            elif code in LIBRARY_BLOCKERS:
                _repair_library(attempt, job, code)
            else:
                st.warning("برای ادامهٔ همین اجرا، اتصال مدل زبانی را آماده کنید.")
                if isinstance(error, dict) and error.get("message_fa"):
                    st.text(error["message_fa"])
                model_repair(attempt, job_id, job["version"])
    _budget(job)
    _technical_details(attempt, job_id, job)
    if not job.get("cancel_requested") and st.button("لغو اجرا", key="cancel-run"):
        if attempt("POST", f"/jobs/{job_id}/cancel", {"expected_job_version": job["version"]}):
            st.rerun()


def _close_execution():
    st.session_state.pop("job_id", None)
    st.session_state.pop("run-problem-summary", None)


def _history(attempt, open_problem):
    st.title("اجراهای من")
    st.caption("اجرای فعال یا نتیجهٔ یک مسئلهٔ قبلی را باز کنید.")
    offset = st.session_state.get("runs-history-offset", 0)
    jobs = attempt("GET", f"/jobs?limit={HISTORY_PAGE_SIZE}&include_problem_summary=true&offset={offset}")
    if jobs is None:
        return
    if not jobs:
        st.info("هنوز اجرایی ثبت نشده است." if not offset else "در این صفحه اجرایی نیست.")
    st.button("مسئلهٔ جدید", type="primary", on_click=open_problem)
    with st.container(key="run-history"):
        for job in jobs:
            identifier = job["job_id"]
            description, action = st.columns([4, 1], vertical_alignment="center")
            description.text(job.get("problem_summary") or f"اجرای {identifier[:8]}")
            status = JOB_LABELS.get(job["job_status"], "وضعیت نامشخص")
            if job["job_status"] == "completed" and job.get("solution_status") == "valid_best_found":
                status = "جواب معتبر پیدا شد"
            method = POLICY_LABELS.get(job.get("policy", {}).get("selection_policy"), "اجرای بهینه‌سازی")
            created_at = job.get("created_at")
            when = datetime.fromisoformat(created_at).astimezone().strftime("%Y/%m/%d · %H:%M") if created_at else ""
            description.caption(" · ".join(filter(None, (status, method, when))))
            if action.button("باز کردن اجرا", key=f"open:{identifier}"):
                st.session_state["job_id"] = identifier
                st.session_state["run-problem-summary"] = job.get("problem_summary")
                st.rerun()
            st.divider()
    if offset or len(jobs) == HISTORY_PAGE_SIZE:
        previous, following = st.columns(2)
        if previous.button("اجراهای جدیدتر", disabled=offset == 0):
            st.session_state["runs-history-offset"] = max(0, offset - HISTORY_PAGE_SIZE)
            st.rerun()
        if following.button("اجراهای قدیمی‌تر", disabled=len(jobs) < HISTORY_PAGE_SIZE):
            st.session_state["runs-history-offset"] = offset + HISTORY_PAGE_SIZE
            st.rerun()


def executions_page(attempt, open_problem, model_repair, show_report):
    job_id = st.session_state.get("job_id")
    if not job_id:
        _history(attempt, open_problem)
        return

    st.button("بازگشت به اجراهای من", type="tertiary", on_click=_close_execution)
    initial = attempt("GET", f"/jobs/{job_id}")
    if not initial:
        return
    if initial["job_status"] in TERMINAL_STATES:
        show_report(job_id, initial)
        _technical_details(attempt, job_id, initial)
        return
    if initial["job_status"] not in ACTIVE_STATES:
        _monitor(attempt, initial, model_repair)
        return

    @st.fragment(run_every=1)
    def refresh():
        job = attempt("GET", f"/jobs/{job_id}")
        if job:
            if job["job_status"] not in ACTIVE_STATES:
                st.rerun()
            _monitor(attempt, job, model_repair)

    refresh()
