"""Guided problem setup with a draft independent of the rendered widgets."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from uuid import uuid4

import pandas as pd
import streamlit as st

from p_mc_optimizer.domain.models import (
    PILOT_COUNTS, PILOT_EVALUATIONS, PILOT_REPEATS, PREFLIGHT_EVALUATIONS,
    PROFILE_EVALUATIONS, VERIFICATION_EVALUATIONS, CreateJob, NaturalLanguageInput,
    ProblemInput, strict_json_loads,
)
from p_mc_optimizer.research.contracts import CreateJobV2
from p_mc_optimizer.ui.catalog import CATALOG_ROUTE
from p_mc_optimizer.ui.web_research import SEARCH_ROUTE, SOURCES_ROUTE, profile_reference

DRAFT_KEY = "problem-draft"
RESET_KEY = "problem-draft-reset"
WIDGET_PREFIX = "_wizard-"
STEP_LABELS = ("تعریف مسئله", "انتخاب روش", "مرور و شروع")
PERSIAN_DIGITS = "۱۲۳"
JSON_LIMIT = 256 * 1024
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫−", "01234567890123456789.-")
REAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?\Z")

WEB = "web_research_then_pilot"
CATALOG = "catalog_llm_then_pilot"
CATALOG_FIXED = "catalog_fixed"
FIXED = "fixed_solver"
V2_POLICIES = {WEB, CATALOG, CATALOG_FIXED}
LLM_POLICIES = {WEB, CATALOG, "llm_shortlist_then_pilot", "llm_only"}
METHOD_LABELS = {
    WEB: "پژوهش وب و مقایسهٔ روش‌ها",
    CATALOG: "پیشنهاد از کتابخانهٔ موجود",
    "manual": "انتخاب روش توسط من",
    FIXED: "اجرای روش ثابت",
    CATALOG_FIXED: "اجرای روش تأییدشدهٔ کاتالوگ",
    "llm_shortlist_then_pilot": "مشاور مدل و آزمون کوتاه",
    "llm_only": "مشاور مدل بدون آزمون کوتاه",
    "random_solver": "روش تصادفی",
    "rule_based": "انتخاب با قاعدهٔ ثابت",
    "pilot_only": "مقایسهٔ چهار روش پایه",
}
METHOD_CAPTIONS = {
    WEB: "منابع مجاز بررسی می‌شوند و سه روش در آزمون کوتاه مقایسه می‌شوند. مدل زبانی لازم است و پژوهش زنده از Tavily استفاده می‌کند.",
    CATALOG: "مدل از کتابخانه پیشنهاد می‌دهد و سه روش آزمایش می‌شوند. این مسیر به مدل زبانی نیاز دارد و جست‌وجوی وب ندارد.",
    "manual": "یک روش قابل اجرا را خودتان انتخاب می‌کنید. ورودی تابع و متغیرها را می‌توانید محلی حل کنید.",
}
SOLVER_LABELS = {
    "de.original": "تکامل تفاضلی (DE)",
    "pso.original": "ازدحام ذرات (PSO)",
    "abc.original": "کلونی زنبور مصنوعی (ABC)",
    "cma_es.mealpy": "راهبرد تکاملی CMA-ES",
}
WEB_MODES = {"live": "جست‌وجوی زنده", "cache_only": "فقط شواهد ذخیره‌شده"}
WEB_FAILURES = {"stop": "توقف و نمایش علت", "continue_with_catalog": "ادامه با مشاور کاتالوگ"}
SENSES = {"minimize": "کمینه‌سازی", "maximize": "بیشینه‌سازی"}
INPUT_TABS = {"text": "با زبان خودم", "structured": "تابع و متغیرها"}
EXAMPLES = {"min": "تابع درجه‌دو · کمینه", "max": "تابع درجه‌دو · بیشینه", "text": "نمونهٔ فارسی"}
VARIABLE_COLUMNS = {
    "name": "شناسهٔ متغیر", "lower": "کران پایین", "upper": "کران بالا",
    "label_fa": "نام فارسی", "unit": "واحد",
}
QUADRATIC = {
    "schema_version": "1.0",
    "variables": [
        {"name": name, "label_fa": None, "type": "real", "lower": -5.0, "upper": 5.0, "unit": None}
        for name in ("x1", "x2")
    ],
    "objective": {
        "sense": "minimize", "unit": None,
        "source": {"kind": "expression", "expression": "(x1-2)**2+(x2+1)**2"},
    },
    "parameters": {},
    "constraints": [],
    "deterministic": True,
}
NATURAL_FA = (
    "تابع زیر را کمینه کنید. هر دو متغیر x1 و x2 حقیقی و در بازهٔ بستهٔ منفی ۵ تا ۵ هستند."
    "\n\n(x1-2)**2 + (x2+1)**2"
)


def _table_rows(problem):
    return [
        {"name": v["name"], "lower": str(v["lower"]), "upper": str(v["upper"]),
         "label_fa": v.get("label_fa") or "", "unit": v.get("unit") or ""}
        for v in problem["variables"]
    ]


def reset_wizard(problem=None):
    """Start a fresh draft, optionally initialized from a prior run's input."""
    st.session_state[RESET_KEY] = problem


def _draft():
    return st.session_state[DRAFT_KEY]


def _initialize_draft():
    previous = st.session_state.pop(RESET_KEY) if RESET_KEY in st.session_state else None
    for key in list(st.session_state):
        if key.startswith(WIDGET_PREFIX):
            del st.session_state[key]
    draft = {"step": 1, "mode": "text", "values": {}}
    if previous and previous["kind"] == "natural_language":
        draft["values"]["text"] = previous["text"]
    elif previous and previous["kind"] == "structured_problem":
        problem = previous["problem"]
        objective = problem["objective"]
        if objective["source"]["kind"] == "expression":
            draft["mode"] = "structured"
            draft["values"].update({
                "rows": _table_rows(problem), "sense": objective["sense"],
                "formula": objective["source"]["expression"], "unit": objective.get("unit") or "",
                "parameters": json.dumps(problem["parameters"], ensure_ascii=False),
            })
        else:
            draft["mode"] = "json"
            draft["json_bytes"] = json.dumps(problem, ensure_ascii=False).encode("utf-8")
            draft["json_name"] = "مسئلهٔ اجرای قبلی"
    st.session_state[DRAFT_KEY] = draft


def _save_field(name, key):
    _draft()["values"][name] = st.session_state[key]
    if name == "method":
        _draft()["values"]["expert_policy"] = None
        st.session_state.pop(f"{WIDGET_PREFIX}expert_policy", None)


def _field(widget, name, *args, default, **kwargs):
    key = f"{WIDGET_PREFIX}{name}"
    if key not in st.session_state:
        st.session_state[key] = _draft()["values"].get(name, default)
    value = widget(*args, key=key, on_change=_save_field, args=(name, key), **kwargs)
    _draft()["values"][name] = value
    return value


def _go(step):
    _draft()["step"] = step
    if step != 3:
        _draft().pop("review", None)
    st.rerun()


def _set_mode(mode):
    draft = _draft()
    draft["mode"] = mode
    st.session_state.pop(f"{WIDGET_PREFIX}input_tab", None)
    if mode in INPUT_TABS:
        draft["values"]["input_tab"] = mode
    elif mode == "example":
        draft["values"]["method"] = "manual"
        draft["values"]["manual_policy"] = FIXED
        draft["values"]["solver"] = "de.original"
        draft["values"]["expert_policy"] = None


def _steps(step):
    rows = []
    for index, label in enumerate(STEP_LABELS, 1):
        if index == step:
            attributes = 'class="wizard-step wizard-step-current" aria-current="step"'
        elif index < step:
            attributes = 'class="wizard-step wizard-step-done"'
        else:
            attributes = 'class="wizard-step"'
        rows.append(f'<li {attributes}><span class="wizard-step-number" aria-hidden="true">'
                    f'{PERSIAN_DIGITS[index-1]}</span><span>{label}</span></li>')
    st.html(
        '<ol class="wizard-steps" aria-label="مراحل ساخت اجرا">' + "".join(rows) + '</ol>'
        f'<p class="wizard-steps-compact">مرحلهٔ {PERSIAN_DIGITS[step-1]} از ۳ · {STEP_LABELS[step-1]}</p>'
    )


def _actions(forward, *, back=None, disabled=False):
    with st.container(key="wizard-actions"):
        right, left = st.columns([1, 1], vertical_alignment="center")
        if back and right.button(back[0], key=f"wizard-back-{back[1]}"):
            _go(back[1])
        return left.button(forward, type="primary", disabled=disabled)


def _parse_number(value):
    normalized = str(value).strip().translate(DIGITS)
    if not REAL.fullmatch(normalized):
        raise ValueError("کران هر متغیر را به‌صورت عدد، بدون جداکنندهٔ هزارگان وارد کنید.")
    return float(normalized)


def _structured_input():
    values = _draft()["values"]
    table_key = f"{WIDGET_PREFIX}variables"
    # Streamlit keeps edits as deltas against the original table. Keep that base
    # while mounted and use the last edited rows as the base on remount.
    if table_key not in st.session_state:
        values["table_base"] = deepcopy(values.get("rows", _table_rows(QUADRATIC)))
    edited = st.data_editor(
        pd.DataFrame(values["table_base"], columns=list(VARIABLE_COLUMNS)),
        num_rows="dynamic", key=table_key, hide_index=True, width="stretch",
        column_config=VARIABLE_COLUMNS,
    )
    rows = edited.fillna("").to_dict("records")
    values["rows"] = rows
    sense = _field(st.radio, "sense", "هدف", list(SENSES), horizontal=True,
                   format_func=SENSES.__getitem__, default="minimize")
    with st.container(key="wizard-formula"):
        expression = _field(st.text_input, "formula", "عبارت تابع هدف",
                            default=QUADRATIC["objective"]["source"]["expression"],
                            help="برای توان از ** و برای ضرب از * استفاده کنید.")
    return rows, sense, expression


def _structured_problem(fields, unit, parameters):
    rows, sense, expression = fields
    return ProblemInput.model_validate({
        "schema_version": "1.0",
        "variables": [
            {"name": row["name"], "type": "real", "lower": _parse_number(row["lower"]),
             "upper": _parse_number(row["upper"]), "label_fa": row["label_fa"] or None,
             "unit": row["unit"] or None}
            for row in rows
        ],
        "objective": {"sense": sense, "unit": unit or None, "source": {
            "kind": "expression", "expression": expression.translate(DIGITS),
        }},
        "parameters": strict_json_loads(parameters.translate(DIGITS), limit=JSON_LIMIT),
        "constraints": [], "deterministic": True,
    }).model_dump(mode="json")


def _clear_upload():
    draft = _draft()
    draft.pop("json_bytes", None)
    draft.pop("json_name", None)
    draft.pop("json_error", None)
    draft["upload_revision"] = draft.get("upload_revision", 0) + 1


def _save_upload(key):
    upload = st.session_state[key]
    draft = _draft()
    if upload is None:
        _clear_upload()
        return
    draft.pop("json_bytes", None)
    draft.pop("json_name", None)
    draft.pop("json_error", None)
    if upload.size > JSON_LIMIT:
        draft["json_error"] = "اندازهٔ فایل JSON باید حداکثر ۲۵۶ کیبی‌بایت باشد."
    else:
        draft["json_bytes"] = upload.getvalue()
        draft["json_name"] = upload.name


def _parse_problem(mode, source, unit, parameters):
    if mode == "text" or (mode == "example" and source == "text"):
        text = source if mode == "text" else NATURAL_FA
        if not text.strip():
            raise ValueError("توضیح مسئله را وارد کنید یا از یک مثال آماده استفاده کنید.")
        return NaturalLanguageInput(kind="natural_language", text=text).model_dump(mode="json")
    if mode == "structured":
        model = _structured_problem(source, unit, parameters)
    elif mode == "json":
        if not source:
            raise ValueError("فایل JSON مسئله را انتخاب کنید.")
        model = ProblemInput.model_validate(strict_json_loads(source, limit=JSON_LIMIT)).model_dump(mode="json")
    else:
        model = deepcopy(QUADRATIC)
        if source == "max":
            model["objective"]["sense"] = "maximize"
            model["objective"]["source"]["expression"] = "7-((x1-2)**2+(x2+1)**2)"
    return {"kind": "structured_problem", "problem": model}


def _error_message(error):
    if hasattr(error, "errors"):
        return error.errors(include_input=False)[0]["msg"]
    if isinstance(error, UnicodeDecodeError):
        return "فایل JSON باید با کدگذاری UTF-8 ذخیره شود."
    return str(error).splitlines()[0]


def _definition(attempt):
    draft = _draft()
    st.caption("حل مسئله")
    st.title("چه مسئله‌ای را می‌خواهید حل کنید؟")
    st.caption("هدف، متغیرها و محدودهٔ آن‌ها را بنویسید.")
    _steps(1)
    mode = draft["mode"]
    if mode in INPUT_TABS:
        with st.container(key="wizard-input-types"):
            mode = _field(st.radio, "input_tab", "نوع ورودی", list(INPUT_TABS), default=mode,
                          format_func=INPUT_TABS.__getitem__, horizontal=True, label_visibility="collapsed")
            draft["mode"] = mode
    if mode == "text":
        source = _field(st.text_area, "text", "توضیح مسئله", default="",
                        placeholder="هدف مسئله، متغیرها و محدودهٔ هرکدام را اینجا بنویسید.",
                        height=180, max_chars=16000)
        st.caption("برای مثال، بگویید چه چیزی باید کمینه یا بیشینه شود.")
    elif mode == "structured":
        source = _structured_input()
    elif mode == "json":
        key = f"{WIDGET_PREFIX}json-{draft.get('upload_revision', 0)}"
        st.file_uploader("فایل JSON مسئله", type=["json"], key=key, on_change=_save_upload, args=(key,),
                         help="فایل UTF-8 با اندازهٔ حداکثر ۲۵۶ کیبی‌بایت.")
        source = draft.get("json_bytes")
        if source:
            st.text(f"فایل انتخابی · {draft['json_name']}")
            st.button("حذف فایل انتخابی", on_click=_clear_upload, type="tertiary")
        if error := draft.get("json_error"):
            st.error(error)
    else:
        source = _field(st.selectbox, "example", "نمونهٔ آموزشی", list(EXAMPLES), default="min",
                        format_func=EXAMPLES.__getitem__)
        st.caption("نمونه‌های درجه‌دو را می‌توانید با روش محلی و بدون کلید API اجرا کنید.")

    if mode != "example":
        st.button("استفاده از مثال آماده", type="tertiary", on_click=_set_mode, args=("example",))
    with st.expander("ورودی پیشرفته", expanded=mode == "json"):
        if mode in ("json", "example"):
            st.button("بازگشت به ورودی مسئله", on_click=_set_mode, args=("text",))
        if mode != "json":
            st.button("بارگذاری فایل JSON", on_click=_set_mode, args=("json",))
        unit = parameters = None
        if mode == "structured":
            unit = _field(st.text_input, "unit", "واحد تابع هدف (اختیاری)", default="")
            with st.container(key="wizard-parameters"):
                parameters = _field(st.text_input, "parameters", "پارامترهای تابع به‌صورت JSON", default="{}")

    natural = mode == "text" or (mode == "example" and source == "text")
    if natural:
        st.caption("متن مسئله پس از شروع اجرا به سرویس مدل ارسال می‌شود.")
    if _actions("ادامه: انتخاب روش"):
        try:
            draft["problem"] = _parse_problem(mode, source, unit, parameters)
        except ValueError as error:
            st.error(_error_message(error))
        else:
            _go(2)
    if natural:
        model = attempt("GET", "/settings/llm/profile")
        if model is not None:
            ready = _model_ready(model.get("profile") or {}, model.get("credential_status") or {})
            st.caption("مدل زبانی آماده است." if ready else "برای حل با توضیح متنی، اتصال مدل زبانی را آماده کنید.")


def _catalog_route(policy):
    params = "limit=250" if policy == CATALOG_FIXED else "pilot_500_compatible=true&limit=1"
    return f"{CATALOG_ROUTE}?status=qualified&{params}"


def _catalog_configs(attempt, catalog):
    if not catalog:
        return {}
    rows = list(catalog["items"])
    for offset in range(250, catalog["total"], 250):
        page = attempt("GET", f"{_catalog_route(CATALOG_FIXED)}&offset={offset}")
        if not page or page["catalog_hash"] != catalog["catalog_hash"]:
            st.warning("کتابخانه تغییر کرده است. فهرست روش‌ها را دوباره باز کنید.")
            return {}
        rows.extend(page["items"])
    return {row["config_id"]: row for row in rows if row.get("config_id")}


def _configuration(attempt, policy, mode):
    """Read only metadata for the integrations this particular request needs."""
    model = (attempt("GET", "/settings/llm/profile") or {}) if policy in LLM_POLICIES else {}
    search = (attempt("GET", SEARCH_ROUTE) or {}) if policy == WEB and mode == "live" else {}
    sources = (attempt("GET", SOURCES_ROUTE) or {}) if policy == WEB else {}
    catalog = (attempt("GET", _catalog_route(policy)) or {}) if policy in V2_POLICIES else {}
    return {
        "model_profile": model.get("profile") or {},
        "model_status": model.get("credential_status") or {},
        "search_profile": search.get("profile") or {},
        "search_ready": bool(search.get("ready")),
        "search_generation": search.get("credential_generation"),
        "source_revision": sources.get("revision"),
        "sources": [row for row in sources.get("sources", []) if row["enabled"]],
        "catalog": catalog,
    }


def _binding(config):
    return (
        profile_reference(config["model_profile"]),
        config["model_status"].get("credential_generation"),
        profile_reference(config["search_profile"]),
        config["search_generation"],
        config["source_revision"],
        config["catalog"].get("catalog_hash"),
    )


def _model_ready(profile, status):
    return profile.get("test_status") == "passed" and status.get("available")


def _requirements(policy, mode, config, *, open_connections, open_library):
    ready = True
    if policy in LLM_POLICIES and not _model_ready(config["model_profile"], config["model_status"]):
        ready = False
        st.warning("برای این روش، کلید مدل زبانی و آزمون موفق اتصال لازم است.")
        st.button("تنظیم اتصال مدل", on_click=open_connections, args=("model",))
    if policy == WEB and mode == "live" and not config["search_ready"]:
        ready = False
        st.warning("برای جست‌وجوی زنده، اتصال Tavily را آماده کنید.")
        st.button("تنظیم Tavily", on_click=open_connections, args=("search",))
    if policy == WEB and not config["sources"]:
        ready = False
        st.warning("برای پژوهش، دست‌کم یک منبع مجاز فعال کنید.")
    catalog_min = 1 if policy == CATALOG_FIXED else 3
    catalog_missing = policy in V2_POLICIES and config["catalog"].get("total", 0) < catalog_min
    if catalog_missing:
        ready = False
        st.warning("کتابخانه هنوز پیکربندی‌های تأییدشدهٔ کافی برای این روش ندارد.")
    if catalog_missing or (policy == WEB and not config["sources"]):
        st.button("باز کردن منابع و الگوریتم‌ها", on_click=open_library)
    return ready


def _manual_policy(natural):
    if natural:
        return FIXED
    labels = {FIXED: "روش‌های پایه", CATALOG_FIXED: "روش‌های تأییدشدهٔ کاتالوگ"}
    return _field(st.radio, "manual_policy", "روش‌ها را از کجا انتخاب کنید؟", list(labels), default=FIXED,
                  format_func=labels.__getitem__, horizontal=True)


def _method_options(main_policy):
    with st.expander("تنظیمات پیشرفته", expanded=bool(_draft()["values"].get("expert_policy"))):
        expert = _field(st.selectbox, "expert_policy", "روش تخصصی",
                        [None, "llm_shortlist_then_pilot", "llm_only", "random_solver", "rule_based", "pilot_only"],
                        default=None, format_func=lambda p: METHOD_LABELS[p] if p else "روش انتخاب‌شده در بالا")
        policy = expert or main_policy
        numeric = {
            "max_objective_evaluations": _field(st.number_input, "budget", "سقف ارزیابی تابع هدف", 1002, 1_000_000, default=20_000),
            "max_compute_seconds": _field(st.number_input, "seconds", "مهلت محاسبات عددی (ثانیه)", 11, 3600, default=300),
            "master_seed": _field(st.number_input, "seed", "بذر تصادفی", 0, 2**32 - 1, default=1405),
            "explain_via_llm": _field(st.checkbox, "explain", "توضیح نتیجه با مدل زبانی", default=True)
            if policy in LLM_POLICIES else False,
        }
        web = {}
        if policy == WEB:
            mode = _field(st.radio, "web_mode", "حالت پژوهش", list(WEB_MODES), default="live",
                          format_func=WEB_MODES.__getitem__)
            failure = _field(st.selectbox, "web_failure", "رفتار در صورت شکست پژوهش", list(WEB_FAILURES), default="stop",
                             format_func=WEB_FAILURES.__getitem__)
            age = _field(st.number_input, "cache_age", "حداکثر عمر شواهد ذخیره‌شده (ساعت)", 1, 720, default=24)
            web = {"research_mode": mode, "on_research_failure": failure, "max_cache_age_hours": age}
    if expert:
        st.caption(f"روش تخصصی انتخاب‌شده · {METHOD_LABELS[expert]}")
    return policy, numeric, web


def _clear_unavailable(name, choices):
    values = _draft()["values"]
    if values.get(name) not in choices:
        values.pop(name, None)
        st.session_state.pop(f"{WIDGET_PREFIX}{name}", None)


def _selected_solver(attempt, policy, config, natural):
    if natural:
        return None, None
    if policy == FIXED:
        solver = _field(st.selectbox, "solver", "روش حل", list(SOLVER_LABELS), default="de.original",
                        format_func=SOLVER_LABELS.__getitem__)
        return solver, None
    if policy == CATALOG_FIXED:
        options = _catalog_configs(attempt, config["catalog"])
        if options:
            _clear_unavailable("fixed_config", options)
            chosen = _field(st.selectbox, "fixed_config", "روش تأییدشده", [None, *options], default=None,
                            format_func=lambda key: options[key]["class_name"] if key else "یک روش انتخاب کنید")
            return None, options.get(chosen)
    return None, None


def _pilot_evaluations(policy):
    count = 3 if policy in (WEB, CATALOG) else PILOT_COUNTS.get(policy, 0)
    return count * PILOT_REPEATS * PILOT_EVALUATIONS


def _payload(problem, policy, numeric, web, config, solver, fixed):
    llm_profile = profile_reference(config["model_profile"]) if policy in LLM_POLICIES else None
    if policy in V2_POLICIES:
        request = CreateJobV2.model_validate({
            "schema_version": "2.0", "input": problem, "llm_profile": llm_profile,
            "search_profile": profile_reference(config["search_profile"]),
            "source_revision": config["source_revision"], "catalog_hash": config["catalog"]["catalog_hash"],
            "policy": {
                "policy_version": "2.0", "portfolio_id": "continuous_catalog_v2", "selection_policy": policy,
                "fixed_implementation_id": fixed["implementation_id"] if fixed else None,
                "fixed_config_id": fixed["config_id"] if fixed else None,
                **numeric, **web,
            },
        })
    else:
        request = CreateJob.model_validate({
            "input": problem, "llm_profile": llm_profile,
            "policy": {
                "policy_version": "1.0", "portfolio_id": "continuous_v1", "selection_policy": policy,
                "fixed_algorithm_id": solver, **numeric,
            },
        })
    return request.model_dump(mode="json")


def _method(attempt, open_connections, open_library):
    draft = _draft()
    problem = draft["problem"]
    natural = problem["kind"] == "natural_language"
    st.caption("حل مسئله")
    st.title("چطور روش حل را انتخاب کنیم؟")
    st.caption("میزان کمک برنامه را انتخاب کنید.")
    _steps(2)
    with st.container(key="wizard-methods"):
        method = _field(st.radio, "method", "روش انتخاب حل", list(METHOD_CAPTIONS),
                        default=WEB if natural else "manual", format_func=METHOD_LABELS.__getitem__,
                        captions=list(METHOD_CAPTIONS.values()), label_visibility="collapsed", width="stretch")
    main_policy = _manual_policy(natural) if method == "manual" else method
    details = st.container()
    policy, numeric, web = _method_options(main_policy)
    mode = web.get("research_mode")
    config = _configuration(attempt, policy, mode)
    with details:
        solver, fixed = _selected_solver(attempt, policy, config, natural)
        ready = _requirements(policy, mode, config, open_connections=open_connections, open_library=open_library)
        valid_policy = not natural or policy in LLM_POLICIES
        if not valid_policy:
            st.info("این روش به ورودی تابع و متغیرها نیاز دارد. نوع ورودی یا روش را تغییر دهید.")
        valid_choice = policy != CATALOG_FIXED or bool(fixed)
        needed = PREFLIGHT_EVALUATIONS + PROFILE_EVALUATIONS + _pilot_evaluations(policy) + VERIFICATION_EVALUATIONS + 500
        valid_budget = numeric["max_objective_evaluations"] >= needed
        if not valid_budget:
            st.warning(f"برای این روش، سقف ارزیابی را در تنظیمات پیشرفته دست‌کم روی {needed:,} قرار دهید.")
        if ready and policy in LLM_POLICIES:
            st.caption("اتصال‌های لازم آماده‌اند.")
    if _actions("ادامه: مرور درخواست", back=("بازگشت به مسئله", 1),
                disabled=not (ready and valid_policy and valid_choice and valid_budget)):
        try:
            payload = _payload(problem, policy, numeric, web, config, solver, fixed)
        except ValueError as error:
            st.error(_error_message(error))
        else:
            draft["review"] = {
                "payload": payload, "binding": _binding(config), "config": config,
                "solver_label": SOLVER_LABELS[solver] if solver else fixed["class_name"] if fixed else None,
            }
            _go(3)


def _problem_summary(problem):
    if problem["kind"] == "natural_language":
        with st.container(height=150, border=False):
            st.text(problem["text"])
        st.caption("صورت‌بندی متن مسئله پس از شروع اجرا انجام می‌شود.")
        return
    model = problem["problem"]
    objective = model["objective"]
    st.text(f"{SENSES[objective['sense']]} تابع هدف با {len(model['variables'])} متغیر")
    source = objective["source"]
    st.code(source.get("expression") or source["function_id"], language=None)
    with st.expander("متغیرها و کران‌ها"):
        st.dataframe(_table_rows(model), hide_index=True, width="stretch", column_config=VARIABLE_COLUMNS)


def _request_summary(review):
    payload = review["payload"]
    policy = payload["policy"]
    method = policy["selection_policy"]
    config = review["config"]
    st.subheader("مسئلهٔ شما")
    _problem_summary(payload["input"])
    st.divider()
    st.subheader("روش و منابع")
    st.text(METHOD_LABELS[method])
    if review["solver_label"]:
        st.text(review["solver_label"])
    if method == WEB:
        st.text(f"{WEB_MODES[policy['research_mode']]} · {WEB_FAILURES[policy['on_research_failure']]}")
        st.text("منابع مجاز · " + "، ".join(row["label"] for row in config["sources"]))
        if policy["research_mode"] == "cache_only":
            st.caption(f"حداکثر عمر شواهد ذخیره‌شده {policy['max_cache_age_hours']} ساعت است.")
    st.divider()
    st.subheader("محدودیت اجرا")
    budget = policy["max_objective_evaluations"]
    pilot = _pilot_evaluations(method)
    initial = PREFLIGHT_EVALUATIONS + PROFILE_EVALUATIONS
    main = budget - initial - pilot - VERIFICATION_EVALUATIONS
    st.text(f"سقف ارزیابی تابع هدف {budget:,} · مهلت عددی {policy['max_compute_seconds']:,} ثانیه")
    st.caption(f"آماده‌سازی {initial:,} · آزمون کوتاه {pilot:,} · اجرای اصلی {main:,} · بازبینی {VERIFICATION_EVALUATIONS:,}")
    if limits := config["search_profile"].get("limits"):
        st.caption(f"حداکثر {limits['max_search_attempts']} تلاش جست‌وجو، "
                   f"{limits['max_http_requests']} درخواست وب و {limits['active_timeout_seconds']:g} ثانیه پژوهش")
    if config["model_profile"]:
        profile = config["model_profile"]
        st.text(f"سرویس مدل · {profile['base_url']} · {profile['model']}")
        st.caption("سرویس مدل اطلاعات مسئله را برای صورت‌بندی یا انتخاب روش دریافت می‌کند.")
    if config["search_profile"]:
        st.text(f"جست‌وجوی Tavily · {config['search_profile']['endpoint']}")
        st.caption("جست‌وجو بر اساس ویژگی‌های کلی مسئله و در منابع مجاز انجام می‌شود.")


def _review(attempt, open_execution, open_connections, open_library):
    draft = _draft()
    review = draft["review"]
    payload = review["payload"]
    policy = payload["policy"]
    method = policy["selection_policy"]
    mode = policy.get("research_mode")
    st.caption("حل مسئله")
    st.title("مرور درخواست")
    st.caption("مسئله و تنظیمات اجرا را مرور کنید.")
    _steps(3)
    with st.container(key="wizard-review"):
        _request_summary(review)
    current = _configuration(attempt, method, mode)
    ready = _requirements(method, mode, current, open_connections=open_connections, open_library=open_library)
    same = _binding(current) == review["binding"]
    if not same:
        st.warning("تنظیمات یا منابع از زمان مرور تغییر کرده‌اند. به انتخاب روش برگردید و درخواست را دوباره مرور کنید.")
    if ready and same and method in LLM_POLICIES:
        st.caption("اتصال‌های لازم آماده‌اند.")
    start = "شروع پژوهش و حل" if method == WEB else "شروع حل" if method in LLM_POLICIES else "شروع حل محلی"
    if _actions(start, back=("بازگشت به انتخاب روش", 2), disabled=not (ready and same)):
        pending = draft.get("pending_submission")
        if not pending or pending["payload"] != payload:
            pending = {"payload": payload, "key": str(uuid4())}
            draft["pending_submission"] = pending
        if created := attempt("POST", "/jobs", payload, idempotency_key=pending["key"]):
            draft.pop("pending_submission", None)
            draft.pop("review", None)
            draft["step"] = 1
            open_execution(created["job_id"])


def wizard(attempt, open_execution, open_connections, open_library):
    if DRAFT_KEY not in st.session_state or RESET_KEY in st.session_state:
        _initialize_draft()
    with st.container(key="problem-wizard"):
        step = _draft()["step"]
        if step == 1:
            _definition(attempt)
        elif step == 2:
            _method(attempt, open_connections, open_library)
        else:
            _review(attempt, open_execution, open_connections, open_library)
