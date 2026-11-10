"""Read-only view of the recorded outcome, evidence, and available exports."""
from __future__ import annotations

import json
import re
from html import escape

import streamlit as st

from p_mc_optimizer.reporting import CLAIM_FA, EVALUATION_COLUMNS, csv_bytes, progress_figure
from p_mc_optimizer.ui.web_research import research_report_panel


ALGORITHM_NAMES = {
    "abc": "کلونی زنبور مصنوعی (ABC)",
    "cma_es": "سازگاری ماتریس کوواریانس (CMA-ES)",
    "de": "تکامل تفاضلی (DE)",
    "pso": "ازدحام ذرات (PSO)",
}
ARTIFACT_NAMES = {
    "report_json": ("report.json", "application/json"),
    "report_html": ("report.html", "text/html"),
    "trials_csv": ("trials.csv", "text/csv"),
    "research_json": ("research.json", "application/json"),
    "sources_csv": ("sources.csv", "text/csv"),
}
RESULT_TABS = ["خلاصهٔ جواب", "پژوهش و مقایسه", "جزئیات فنی"]


def _method_label(identifier):
    if not identifier:
        return "—"
    if not identifier.startswith("mealpy:"):
        return ALGORITHM_NAMES.get(identifier.split(".", 1)[0], identifier)
    parts = identifier.split(":", 2)
    module, class_name = parts[1:] if len(parts) == 3 else ("", parts[-1])
    family = module.rsplit(".", 1)[-1].lower()
    family_name = ALGORITHM_NAMES.get(family)
    if class_name.startswith("Original") and family_name:
        return family_name
    if class_name == "CMA_ES":
        return ALGORITHM_NAMES["cma_es"]
    readable = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", class_name).replace("_", " ")
    return f"{family_name} · {readable}" if family_name else readable


def _number(value):
    return "—" if value is None else f"{value:.15g}"


def _problem_spec(report):
    revision = (report.get("problem_revision") or {}).get("spec")
    return revision or (report.get("input") or {}).get("problem") or {}


def _repeat_input(report):
    # Prefer the frozen model: it includes any answers supplied during this run.
    spec = _problem_spec(report)
    if spec:
        return {"kind": "structured_problem", "problem": {key: value for key, value in spec.items()
                                                            if key != "source_map"}}
    original = report.get("input") or {}
    return original if original.get("kind") == "natural_language" and original.get("text") else None


def _result_point(report):
    if report.get("solution_status") == "valid_best_found" and report.get("best_x") is not None:
        return report["best_x"], report.get("objective_raw"), True
    candidate = (report.get("diagnostics") or {}).get("unverified_candidate") or {}
    return candidate.get("x"), candidate.get("objective_raw"), False


def _title(report, job, verified):
    if job["job_status"] != "completed" or report.get("partial_result"):
        return "اجرا کامل نشد"
    if verified:
        return "جواب معتبر پیدا شد"
    return "جواب معتبر پیدا نشد"


def _problem_caption(report):
    spec = _problem_spec(report)
    objective = spec.get("objective") or {}
    source = objective.get("source") or {}
    if value := source.get("expression") or source.get("function_id"):
        sense = {"minimize": "کمینه‌سازی", "maximize": "بیشینه‌سازی"}.get(objective.get("sense"), "تابع هدف")
        value = value[:159] + "…" if len(value) > 160 else value
        st.text(f"{sense} · \u2066{value}\u2069")
        return
    else:
        short = " ".join(((report.get("input") or {}).get("text") or "").split())
    if short:
        st.text(short[:160] + ("…" if len(short) > 160 else ""))


def _values_table(variables, point):
    rows = []
    for index, value in enumerate(point):
        variable = variables[index] if index < len(variables) else {}
        name = variable.get("name") or f"x{index + 1}"
        label = variable.get("label_fa")
        unit = variable.get("unit")
        name_html = f'<bdi dir="ltr">{escape(name)}</bdi>'
        if label and label != name:
            name_html = f"{escape(label)} ({name_html})"
        amount = f'<bdi dir="ltr">{escape(_number(value))}</bdi>'
        if unit:
            amount += " " + escape(unit)
        rows.append(f'<tr><th scope="row">{name_html}</th><td>{amount}</td></tr>')
    st.html('<table class="result-values" aria-label="مقادیر متغیرها">'
            '<thead><tr><th scope="col">متغیر</th><th scope="col">مقدار</th></tr></thead>'
            '<tbody>' + "".join(rows) + '</tbody></table>')


def _summary(report, job, point, objective, verified, tab_key):
    state = job["job_status"]
    if state == "cancelled":
        st.error("اجرا به درخواست کاربر لغو شد.", icon=":material/stop_circle:")
    elif state == "failed":
        st.error("اجرا با خطا پایان یافت.")
    elif report.get("partial_result"):
        st.warning("گزارش این اجرا بخشی از نتیجه را در بر دارد.")
    error = report.get("error")
    if isinstance(error, dict) and error.get("message_fa"):
        st.text(error["message_fa"])

    if point is None:
        st.info("نقطه‌ای برای نمایش در این گزارش ثبت نشده است.")
    else:
        spec = _problem_spec(report)
        with st.container(key="result-summary"):
            variables, target = st.columns([3, 2])
            with variables:
                st.subheader("مقادیر پیشنهادی" if verified else "بهترین جواب ثبت‌شده")
                _values_table(spec.get("variables") or [], point)
            with target:
                st.metric("مقدار تابع هدف", _number(objective))
                goal = spec.get("objective") or {}
                if sense := {"minimize": "کمینه‌سازی", "maximize": "بیشینه‌سازی"}.get(goal.get("sense")):
                    st.caption(f"هدف · {sense}")
                if unit := goal.get("unit"):
                    st.text(f"واحد · {unit}")
        if verified:
            st.success("جواب با محاسبهٔ دوباره تأیید شد.", icon=":material/check_circle:")
            st.caption(CLAIM_FA)
        elif report.get("solution_status") == "validation_failed":
            st.warning("نقطهٔ ثبت‌شده بررسی شد و معیار اعتبارسنجی را برآورده نکرد.")
        else:
            st.warning("این نقطه هنوز تأیید نهایی نشده است.")

    explanation = report.get("explanation") or {}
    solver = report.get("selected_algorithm_id")
    if solver or explanation.get("selection_explanation_fa"):
        st.divider()
        st.subheader("چرا این روش؟")
        if solver:
            st.text(_method_label(solver))
        if line := explanation.get("selection_explanation_fa"):
            if solver:
                line = line.replace(solver, _method_label(solver))
            st.text(line)
        generated = report.get("explanation_status") == "generated"
        st.caption("توضیح مدل زبانی" if generated else "توضیح خودکار برنامه")
    elif explanation.get("summary_fa"):
        st.text(explanation["summary_fa"])
    with st.expander("محدودیت‌های نتیجه"):
        if line := explanation.get("summary_fa"):
            st.text(line)
        for line in explanation.get("limitations_fa") or []:
            st.text(line)
        st.caption(CLAIM_FA)

    research = report.get("research") or {}
    sources = sum(item.get("fetch_status") in {"read", "revalidated", "cached"}
                  for item in research.get("documents") or [])
    tried = {trial.get("algorithm_id") for trial in report.get("trials") or []
             if trial.get("phase") == "pilot" and (trial.get("started_evaluations") or 0) > 0}
    if sources or tried:
        st.divider()
        st.subheader("چه چیزی بررسی شد؟")
        details = [f"{sources} سند قابل استفاده" if sources else None,
                   f"{len(tried)} روش آزمایش‌شده" if tried else None]
        st.caption(" · ".join(filter(None, details)))
        st.button("دیدن شواهد و مقایسه", type="tertiary", key=f"evidence-link:{job['job_id']}",
                  on_click=lambda: st.session_state.update({tab_key: RESULT_TABS[1]}))


def _research_and_trials(report):
    if report.get("schema_version") in {"2.0", "2.1"}:
        research_report_panel(report)
    decision = report.get("decision") or {}
    metrics = {metric["algorithm_id"]: metric for metric in decision.get("metrics", [])}
    pilot_rows = []
    for trial in report.get("trials") or []:
        if trial.get("phase") != "pilot":
            continue
        identifier = trial.get("algorithm_id")
        metric = metrics.get(identifier, {})
        pilot_rows.append({
            "روش": _method_label(identifier), "بذر": trial.get("seed"),
            "ارزیابی": trial.get("started_evaluations"), "مقدار هدف": trial.get("objective_raw"),
            "امتیاز کمینه‌سازی": trial.get("score_for_minimization"), "میانه": metric.get("median_score"),
            "IQR نرمال‌شده": metric.get("normalized_iqr"), "واجد شرایط": metric.get("eligible"),
            "علت": metric.get("reason") or trial.get("error_code"),
        })
    if pilot_rows:
        st.subheader("مقایسهٔ آزمایش‌های کوتاه")
        st.dataframe(pilot_rows, hide_index=True)
    elif report.get("schema_version") not in {"2.0", "2.1"}:
        st.info("برای این اجرا پژوهش وب یا آزمایش مقایسه‌ای ثبت نشده است.")


def _technical_report(report, load_artifact):
    if report.get("progress"):
        st.html(progress_figure(report["progress"]))
        st.caption("برای بیشینه‌سازی، امتیاز کمینه‌سازی برابر منفی مقدار تابع هدف است.")
    detail_labels = [
        ("problem_revision", "مدل مسئله و منشأ فیلدها"), ("decision", "شواهد انتخاب و رتبه‌بندی"),
        ("verification", "بررسی مجدد"), ("solution_origin", "منشأ جواب"), ("trials", "جزئیات اجرای روش‌ها"),
        ("diagnostics", "اطلاعات عیب‌یابی"), ("budget", "حساب بودجه"), ("error", "خطا"),
        ("explanation_error", "خطای تولید توضیح مدل"),
    ]
    for key, label in detail_labels:
        if report.get(key) is not None:
            with st.expander(label):
                st.json(report[key], expanded=False)
    if histories := report.get("execution_history_artifacts"):
        def history_csv():
            def records():
                for artifact_id in histories:
                    for line in load_artifact(artifact_id).splitlines():
                        row = json.loads(line)
                        if row.get("kind") == "result":
                            yield row["record"]
                        elif "evaluation_id" in row:
                            yield row
            return csv_bytes(records(), EVALUATION_COLUMNS)

        st.download_button("دریافت تاریخچهٔ ارزیابی‌ها", history_csv, "evaluation_history.csv", "text/csv")


def _download(artifact, load_artifact, label=None, *, primary=False):
    name, mime = ARTIFACT_NAMES[artifact["kind"]]
    artifact_id = artifact["artifact_id"]
    st.download_button(label or name, lambda: load_artifact(artifact_id), name, mime,
                       type="primary" if primary else "secondary", key=f"download:{artifact_id}")


def report_panel(attempt, job_id, job, open_problem, *, load_artifact):
    item = attempt("GET", f"/jobs/{job_id}/report")
    if not item:
        return
    report = item["report"]
    point, objective, verified = _result_point(report)
    st.title(_title(report, job, verified))
    _problem_caption(report)
    tab_key = f"result-tabs:{job_id}"
    summary, research, technical = st.tabs(RESULT_TABS, key=tab_key, on_change="rerun")
    if summary.open:
        with summary:
            _summary(report, job, point, objective, verified, tab_key)
    if research.open:
        with research:
            _research_and_trials(report)
    if technical.open:
        with technical:
            _technical_report(report, load_artifact)

    artifacts = {artifact["kind"]: artifact for artifact in item.get("artifacts") or []
                 if artifact["kind"] in ARTIFACT_NAMES}
    main_kind = "report_html" if "report_html" in artifacts else "report_json"
    repeat = _repeat_input(report) if job["job_status"] != "completed" or report.get("partial_result") else None
    with st.container(key="result-actions"):
        start, download = st.columns([1, 1])
        if repeat:
            start.button("اجرای تازه با همین مسئله", on_click=open_problem, kwargs={"problem": repeat})
            start.caption("اجرای تازه، درخواست مستقلی ایجاد می‌کند.")
        else:
            start.button("مسئلهٔ جدید", on_click=open_problem)
        if artifact := artifacts.pop(main_kind, None):
            with download:
                label = "دریافت گزارش موجود" if report.get("partial_result") else "دریافت گزارش"
                _download(artifact, load_artifact, label, primary=True)
    if artifacts:
        with st.expander("فرمت‌های دیگر گزارش"):
            for artifact in artifacts.values():
                _download(artifact, load_artifact)
