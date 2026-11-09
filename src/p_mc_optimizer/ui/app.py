"""Persian Streamlit client. All state mutations go through the local API."""

from __future__ import annotations

import json
import os
import re
from uuid import uuid4

import httpx
import pandas as pd
import streamlit as st

from p_mc_optimizer.domain.models import ProblemInput, strict_json_loads
from p_mc_optimizer.reporting import CLAIM_FA, EVALUATION_COLUMNS, csv_bytes, progress_figure
from p_mc_optimizer.ui.research import research


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


DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫−", "01234567890123456789.-")
REAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?\Z")
POLICIES = {
    "llm_shortlist_then_pilot": "مشاور مدل و پایلوت",
    "llm_only": "مشاور مدل بدون پایلوت",
    "fixed_solver": "خط پایه · روش ثابت",
    "random_solver": "خط پایه · روش تصادفی",
    "rule_based": "خط پایه · قاعدهٔ ثابت",
    "pilot_only": "خط پایه · مقایسهٔ چهار روش",
}
LLM_POLICIES = {"llm_shortlist_then_pilot", "llm_only"}
ALGORITHMS = ["abc.original", "cma_es.mealpy", "de.original", "pso.original"]

QUADRATIC = {
    "schema_version": "1.0",
    "variables": [{"name": name, "label_fa": None, "type": "real", "lower": -5.0,
                   "upper": 5.0, "unit": None} for name in ("x1", "x2")],
    "objective": {"sense": "minimize", "unit": None,
                  "source": {"kind": "expression", "expression": "(x1-2)**2+(x2+1)**2"}},
    "parameters": {}, "constraints": [], "deterministic": True,
}
NATURAL_FA = "تابع (x1-2)**2+(x2+1)**2 را کمینه کنید. هر دو متغیر x1 و x2 حقیقی و در بازهٔ بستهٔ [-5, 5] هستند."


def parse_number(value):
    normalized = str(value).strip().translate(DIGITS)
    if not REAL.fullmatch(normalized):
        raise ValueError("عدد را با نقطهٔ اعشاری و بدون جداکنندهٔ هزارگان وارد کنید.")
    return float(normalized)


def problem_form(prefix, *, initial=None):
    initial = initial or QUADRATIC
    rows = pd.DataFrame([{"name": v["name"], "lower": str(v["lower"]), "upper": str(v["upper"]),
                          "label_fa": v.get("label_fa") or "", "unit": v.get("unit") or ""}
                         for v in initial["variables"]])
    edited = st.data_editor(rows, num_rows="dynamic", key=prefix + "_variables", hide_index=True,
                            width="stretch", column_config={
                                "name": "شناسهٔ متغیر", "lower": "کران پایین", "upper": "کران بالا",
                                "label_fa": "برچسب فارسی", "unit": "واحد",
                            })
    objective = initial["objective"]
    sense = st.selectbox("جهت هدف", ["minimize", "maximize"],
                         index=int(objective["sense"] == "maximize"), key=prefix + "_sense",
                         format_func=lambda x: {"minimize": "کمینه‌سازی", "maximize": "بیشینه‌سازی"}[x])
    source = objective["source"]
    expression = st.text_input("عبارت تابع هدف", value=source.get("expression", ""), key=prefix + "_expr",
                               help="از ** برای توان استفاده کنید. ضرب باید با * نوشته شود.")
    unit = st.text_input("واحد تابع هدف (اختیاری)", value=objective.get("unit") or "", key=prefix + "_unit")
    parameters = st.text_input("پارامترها به‌صورت شیء JSON", value=json.dumps(initial["parameters"]),
                               key=prefix + "_params")
    return edited, sense, expression, unit, parameters


def read_form(fields):
    edited, sense, expression, unit, parameters = fields
    variables = [{"name": v["name"], "type": "real", "lower": parse_number(v["lower"]),
                  "upper": parse_number(v["upper"]), "label_fa": v["label_fa"] or None,
                  "unit": v["unit"] or None} for v in edited.fillna("").to_dict("records")]
    return ProblemInput.model_validate({
        "schema_version": "1.0", "variables": variables,
        "objective": {"sense": sense, "unit": unit or None, "source": {
            "kind": "expression", "expression": expression.translate(DIGITS),
        }},
        "parameters": strict_json_loads(parameters.translate(DIGITS)), "constraints": [], "deterministic": True,
    }).model_dump(mode="json")


def new_job():
    st.header("مسئلهٔ جدید")
    mode = st.radio("نوع ورودی", ["متن طبیعی", "فرم ساخت‌یافته", "فایل JSON", "نمونه‌های آماده"], horizontal=True)
    setup = attempt("GET", "/settings/llm/profile")
    profile = (setup or {}).get("profile")
    ready = profile and profile["test_status"] == "passed" and (setup.get("credential_status") or {}).get("available")
    if not ready:
        st.info("برای صورت‌بندی متن یا مشاور مدل، ارائه‌دهنده و کلید را در تنظیمات مدل زبانی ثبت و اتصال را آزمایش کنید.")
    with st.form("new-job"):
        if mode == "متن طبیعی":
            source = st.text_area("متن مسئله", placeholder=NATURAL_FA, max_chars=16000)
        elif mode == "فرم ساخت‌یافته":
            source = problem_form("new")
        elif mode == "فایل JSON":
            source = st.file_uploader("مسئلهٔ JSON با کدگذاری UTF-8", type=["json"],
                                      help="حداکثر ۲۵۶ کیبی‌بایت")
        else:
            source = st.selectbox("نمونهٔ آموزشی", ["تابع درجه‌دو · کمینه", "تابع درجه‌دو · بیشینه", "متن فارسی کامل"])
        natural = mode == "متن طبیعی"
        choices = list(POLICIES)[:2] if natural else list(POLICIES)
        policy = st.selectbox("سیاست انتخاب الگوریتم", choices, index=0 if natural or ready else 2,
                              format_func=POLICIES.__getitem__)
        solver = st.selectbox("الگوریتم در سیاست روش ثابت", ALGORITHMS, index=2)
        columns = st.columns(3)
        budget = columns[0].number_input("سقف FE", 1002, 1000000, 20000)
        seconds = columns[1].number_input("زمان عددی (ثانیه)", 11, 3600, 300)
        seed = columns[2].number_input("بذر", 0, 2**32 - 1, 1405)
        explain = st.checkbox("توضیح از API در سیاست دارای مدل", value=True)
        if profile:
            st.caption(f"مقصد API برای متن و خلاصهٔ شواهد · {profile['base_url']} · {profile['model']}")
        submitted = st.form_submit_button("تحلیل و حل", type="primary")
    if not submitted:
        return
    try:
        if mode == "متن طبیعی" or (mode == "نمونه‌های آماده" and source == "متن فارسی کامل"):
            problem = {"kind": "natural_language", "text": source if mode == "متن طبیعی" else NATURAL_FA}
        else:
            if mode == "فرم ساخت‌یافته":
                model = read_form(source)
            elif mode == "فایل JSON":
                if source is None:
                    raise ValueError("یک فایل JSON انتخاب کنید.")
                model = ProblemInput.model_validate(strict_json_loads(source.getvalue(), limit=256 * 1024)).model_dump(mode="json")
            else:
                model = QUADRATIC
                if source.endswith("بیشینه"):
                    model = {**model, "objective": {"sense": "maximize", "unit": None, "source": {
                        "kind": "expression", "expression": "7-((x1-2)**2+(x2+1)**2)",
                    }}}
            problem = {"kind": "structured_problem", "problem": model}
        if policy in LLM_POLICIES and not ready:
            raise ValueError("پیش از ارسال، اتصال مدل را در تنظیمات آزمایش کنید.")
        payload = {"input": problem, "policy": {
            "policy_version": "1.0", "portfolio_id": "continuous_v1", "selection_policy": policy,
            "fixed_algorithm_id": solver if policy == "fixed_solver" else None,
            "max_objective_evaluations": budget, "max_compute_seconds": seconds, "master_seed": seed,
            "explain_via_llm": explain and policy in LLM_POLICIES,
        }, "llm_profile": {"profile_id": profile["profile_id"], "revision": profile["revision"]}
            if policy in LLM_POLICIES else None}
    except ValueError as exc:
        st.error(str(exc).splitlines()[0])
        return
    pending = st.session_state.get("pending_submission")
    if not pending or pending["payload"] != payload:
        pending = {"payload": payload, "key": str(uuid4())}
        st.session_state.pending_submission = pending
    created = attempt("POST", "/jobs", payload, idempotency_key=pending["key"])
    if created:
        st.session_state.job_id = created["job_id"]
        st.success("درخواست ثبت شد. وضعیت آن در بخش «اجرای حل» قابل مشاهده است.")


def settings(job_id=None, job_version=None):
    st.header("تنظیمات مدل زبانی" if job_id is None else "رفع اتصال همین اجرا")
    st.caption(f"مسیر دادهٔ محلی · {os.environ.get('PMC_DATA_DIR', '')}")
    route = "/settings/llm/profile" + (f"?job_id={job_id}" if job_id else "")
    result = attempt("GET", route)
    if result is None:
        return
    profile = result["profile"] or {}
    status = result["credential_status"] or {}
    if job_id is None:
        preset = st.selectbox("ارائه‌دهنده", ["openai", "custom_openai_compatible"],
                              index=int(profile.get("preset") == "custom_openai_compatible"),
                              format_func=lambda v: "OpenAI" if v == "openai" else "OpenAI-compatible سفارشی")
        with st.form("provider"):
            base_url = st.text_input("نشانی پایه HTTPS",
                                     value="https://api.openai.com/v1" if preset == "openai" else (
                                         profile.get("base_url", "") if profile.get("preset") == preset else ""),
                                     disabled=preset == "openai")
            model = st.text_input("شناسهٔ دقیق مدل", value=profile.get("model", ""))
            modes = ["json_schema", "json_object", "text_json"]
            mode = st.selectbox("شیوهٔ خروجی ساخت‌یافته", modes,
                                index=modes.index(profile.get("response_mode", "json_schema")))
            fields = ["max_completion_tokens", "max_tokens"]
            field = st.selectbox("پارامتر سقف خروجی", fields, disabled=preset == "openai",
                                 index=fields.index(profile.get("token_limit_field", fields[0])) if preset != "openai" else 0)
            max_tokens = st.number_input("سقف توکن خروجی نقش", 256, 16384, profile.get("max_output_tokens", 4096))
            test_tokens = st.number_input("سقف توکن آزمون اتصال", 128, 4096,
                                          profile.get("connection_test_max_output_tokens", 512))
            timeout = st.number_input("مهلت پاسخ API (ثانیه)", 5, 300, profile.get("request_timeout_seconds", 60))
            save = st.form_submit_button("ثبت تنظیمات ارائه‌دهنده")
        if save:
            reply = attempt("PUT", "/settings/llm/profile", {
                "expected_revision": profile.get("revision"), "preset": preset, "base_url": base_url,
                "model": model, "protocol": "chat_completions_v1", "response_mode": mode,
                "token_limit_field": field, "max_output_tokens": max_tokens,
                "connection_test_max_output_tokens": test_tokens, "request_timeout_seconds": timeout,
            })
            if reply:
                st.rerun()
    if not profile:
        return
    st.code(f"{profile['base_url']}\n{profile['model']} · {profile['response_mode']}", language=None)
    st.write("کلید در دسترس است." if status.get("available") else "کلید قابل استفاده در دسترس نیست.")
    st.caption(f"شیوهٔ نگهداری · {status.get('storage_mode') or '—'}")
    target = {"profile_id": profile["profile_id"], "profile_revision": profile["revision"],
              "expected_credential_generation": status.get("credential_generation", 0), "target_job_id": job_id}
    if not result["os_keyring_available"]:
        st.caption("مخزن امن سیستم‌عامل در دسترس نیست. نگهداری در حافظهٔ نشست فعال است.")
    with st.form(f"credential-{job_id}", clear_on_submit=True):
        api_key = st.text_input("کلید API", type="password", autocomplete="off")
        storage = st.selectbox("نگهداری کلید",
                               ["session", "os_keyring"] if result["os_keyring_available"] else ["session"],
                               format_func=lambda v: "حافظهٔ همین نشست" if v == "session" else "مخزن امن سیستم‌عامل")
        submit = st.form_submit_button("ثبت کلید API")
    if submit:
        changed = attempt("PUT", "/settings/llm/credential", {**target, "api_key": api_key, "storage_mode": storage})
        if changed:
            st.rerun()
    if st.button("حذف کلید", disabled=not status.get("configured"), key=f"delete-{job_id}"):
        if attempt("DELETE", "/settings/llm/credential", target):
            st.rerun()
    st.caption("آزمون اتصال یک درخواست کوتاه به مقصد بالا می‌فرستد و ممکن است هزینه داشته باشد.")
    st.write(f"وضعیت آزمون · {profile['test_status']}")
    if profile.get("tested_at"):
        st.code(profile["tested_at"], language=None)
    if st.button("آزمون اتصال", disabled=not status.get("available"), key=f"test-{job_id}"):
        test = attempt("POST", "/settings/llm/test", target, idempotency_key=str(uuid4()))
        if test and test["test_status"] == "passed":
            st.rerun()
        if test and test.get("error"):
            st.error(test["error"]["message_fa"])
    if job_id and st.button("ادامهٔ همین اجرا", disabled=profile["test_status"] != "passed"):
        if attempt("POST", f"/jobs/{job_id}/resume", {"expected_job_version": job_version}):
            st.rerun()


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
    st.caption(CLAIM_FA)
    st.write("اعتبار جواب", report.get("solution_status"))
    st.write("روش منتخب", report.get("selected_algorithm_id"))
    if report.get("solution_origin"):
        st.write("منشأ جواب", report["solution_origin"])
    if report.get("best_x") is not None:
        st.code(f"x = {report['best_x']}\nf(x) = {report['objective_raw']:.15g}", language=None)
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
    for trial in report.get("trials", []):
        with st.expander(f"{trial['algorithm_id']} · {trial['phase']} · {trial['seed']}"):
            st.json(trial)
    st.subheader("توضیح مدل" if report.get("explanation_status") == "generated" else "توضیح قالبی برنامه")
    for k in ("summary_fa", "selection_explanation_fa"):
        if line := (report.get("explanation") or {}).get(k):
            st.text(line)
    for line in (report.get("explanation") or {}).get("limitations_fa", []):
        st.text(line)
    st.caption(f"وضعیت توضیح · {report.get('explanation_status')} | هزینهٔ API · نامشخص")
    st.subheader("دریافت گزارش")
    names = {"report_json": "report.json", "report_html": "report.html", "trials_csv": "trials.csv"}
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
    st.header("اجرای حل")
    job_id = st.session_state.get("job_id")
    if not job_id:
        st.info("از صفحهٔ مسئلهٔ جدید یا تاریخچه یک اجرا را انتخاب کنید.")
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
        st.code(f"{job_id} · {job['job_status']} · {job['phase']}", language=None)
        budget = job["budget"]
        used = budget.get("known_started", 0)
        total = job["policy"]["max_objective_evaluations"]
        st.progress(min(1.0, used / total), text=f"{used:,} / {total:,} FE")
        st.caption(f"اعتبار جواب · {job['solution_status']}")
        if job.get("blocked_reason"):
            st.warning(job["blocked_reason"])
        if job.get("cancel_requested"):
            st.warning("درخواست لغو ثبت شد. وضعیت پس از توقف عددی به‌روزرسانی می‌شود.")
        elif job["job_status"] in {"queued", "running", "needs_input", "needs_configuration"}:
            if st.button("لغو اجرا"):
                if attempt("POST", f"/jobs/{job_id}/cancel", {"expected_job_version": job["version"]}):
                    st.rerun()
        with st.expander("رویدادها و اجرای الگوریتم‌ها"):
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
        model_tab, report_tab = st.tabs(["مدل مسئله", "نتایج"], key=f"job-view:{job_id}", on_change="rerun")
        if model_tab.open:
            with model_tab:
                model_panel(job_id, job)
        if report_tab.open:
            with report_tab:
                report_panel(job_id, job)
        if job["job_status"] == "needs_configuration":
            settings(job_id, job["version"])

    monitor()


def history():
    st.header("تاریخچه")
    jobs = attempt("GET", "/jobs")
    for job in jobs or []:
        if st.button(f"{job['job_status']} · {job['job_id']}", key=job["job_id"]):
            st.session_state.job_id = job["job_id"]
            st.success("اجرا انتخاب شد. گزارش در بخش «اجرای حل» در دسترس است.")


def main():
    st.set_page_config(page_title="SolverScout | بهینه‌ساز محلی", layout="wide")
    st.markdown("""<style>
[data-testid="stMainBlockContainer"], [data-testid="stSidebar"] { direction: rtl; text-align: start; }
pre, code, input, textarea, [data-testid="stDataFrame"] { direction: ltr; unicode-bidi: isolate; }
[data-testid="stHeadingWithActionElements"] { direction: rtl; }
[data-testid="stAppDeployButton"] { display: none; }
</style>""", unsafe_allow_html=True)
    st.title("SolverScout | بهینه‌ساز محلی")
    pages = {"شروع": None, "مسئلهٔ جدید": new_job, "اجرای حل": execution, "تاریخچه": history,
             "تنظیمات مدل زبانی": settings, "آزمایش پژوهشی": lambda: research(attempt)}
    page = st.sidebar.radio("صفحه", list(pages))
    if pages[page]:
        pages[page]()
    else:
        st.header("شروع")
        st.write("یک مسئلهٔ پیوسته و تک‌هدفه وارد کنید و شواهد اجرای الگوریتم‌ها را در گزارش ببینید.")
        status = attempt("GET", "/settings/llm/profile")
        if status is not None:
            st.success("سرویس محلی آماده است.")
            if (status.get("profile") or {}).get("test_status") != "passed" or not (
                status.get("credential_status") or {}
            ).get("available"):
                st.info("مدل زبانی آماده نیست. برای متن طبیعی به تنظیمات مدل زبانی بروید. برای حل عددی مستقل، ورودی ساخت‌یافته و یک خط پایه انتخاب کنید.")
        st.caption(CLAIM_FA)


if __name__ == "__main__":
    main()
