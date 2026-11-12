"""Pure reporting projections and bounded evidence. Never calls an evaluator."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
from statistics import median
from typing import Any
from urllib.parse import urlsplit

from p_mc_optimizer.llm.narrative_models import (
    DerivationRecord, EvidenceRecord, FactRecord, MethodIdentity, OmissionRecord,
    ReportEvidenceV3, SectionRequirement, SECTION_ORDER,
)
from p_mc_optimizer.llm.explanation_context import build_decision_summary
from p_mc_optimizer.reporting.presentation import format_value, method_display_name

CORE_FIELDS = (
    "job_id", "job_status", "schema_version", "input", "problem_revision", "problem_math_hash",
    "evaluation_contract_hash", "policy", "profile", "solver_cards", "candidate_plan", "decision",
    "proposal_source", "proposal_error", "selection_status", "selected_algorithm_id", "selected_config_id",
    "solution_origin", "best_evaluation_id", "best_x", "objective_raw", "score_for_minimization",
    "verification", "verification_error", "solution_status", "optimality_status", "termination_reason",
    "main_success", "partial_result", "trials", "progress", "execution_history_artifacts", "budget", "error",
    "research_snapshot", "research", "research_error", "query_plan", "advisor_plan_v2", "catalog_exclusions",
    "requested_selection_policy", "effective_selection_policy", "numeric_execution",
)


class EvidenceBuildError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_bytes(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def value_hash(value: Any) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


def _json_types(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_types(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_types(item)
        return
    raise EvidenceBuildError("CONTEXT_MISMATCH", "نوع یا مقدار نامعتبر در دادهٔ عددی گزارش ثبت شده است.")


def numerical_core(report: dict) -> dict:
    if type(report) is not dict:
        raise EvidenceBuildError("CONTEXT_MISMATCH", "گزارش عددی معتبر نیست.")
    core = deepcopy({key: report[key] for key in CORE_FIELDS if key in report})
    _json_types(core)
    for name in ("job_id", "job_status", "schema_version", "solution_status", "optimality_status"):
        if name in core and core[name] is not None and type(core[name]) is not str:
            raise EvidenceBuildError("CONTEXT_MISMATCH", "نوع وضعیت یا شناسهٔ گزارش معتبر نیست.")
    for name in ("problem_revision", "policy", "budget", "profile", "decision", "verification"):
        if name in core and core[name] is not None and type(core[name]) is not dict:
            raise EvidenceBuildError("CONTEXT_MISMATCH", "ساختار رکورد گزارش معتبر نیست.")
    for trial in core.get("trials") or []:
        if not isinstance(trial, dict):
            raise EvidenceBuildError("CONTEXT_MISMATCH", "ساختار Trial معتبر نیست.")
        for name in ("started_evaluations", "evaluation_quota", "successful_evaluations", "failed_evaluations"):
            value = trial.get(name)
            if value is not None and (type(value) is not int or value < 0):
                raise EvidenceBuildError("CONTEXT_MISMATCH", "شمار ارزیابی Trial باید عدد صحیح نامنفی باشد.")
        if trial.get("job_id") is not None and trial["job_id"] != core.get("job_id"):
            raise EvidenceBuildError("CONTEXT_MISMATCH", "Trial به اجرای دیگری متصل است.")
        if trial.get("phase") == "pilot" and (type(trial.get("replicate_index")) is not int or trial["replicate_index"] < 0):
            raise EvidenceBuildError("CONTEXT_MISMATCH", "شمارهٔ تکرار پایلوت معتبر نیست.")
    for key in ("objective_raw", "score_for_minimization"):
        value = core.get(key)
        if value is not None and type(value) not in (int, float):
            raise EvidenceBuildError("CONTEXT_MISMATCH", "مقدار تابع باید عدد حقیقی متناهی باشد.")
    for key, value in (core.get("budget") or {}).items():
        if value is not None and (type(value) is not int or value < 0):
            raise EvidenceBuildError("CONTEXT_MISMATCH", "حساب ارزیابی‌های گزارش معتبر نیست.")
    canonical_bytes(core)
    return core


def core_hash(report: dict) -> str:
    return value_hash(numerical_core(report))


def evidence_hash(evidence: ReportEvidenceV3 | dict) -> str:
    data = evidence.model_dump(mode="json") if hasattr(evidence, "model_dump") else deepcopy(evidence)
    data.pop("context_hash", None)
    return value_hash(data)


def _quantile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q
    lo = math.floor(position)
    hi = math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def _clean_text(value):
    # Provider credentials/private filesystem paths never form explanatory evidence.
    text = str(value)
    import re
    text = re.sub(r"\b(?:sk-|tvly-)[A-Za-z0-9_-]{12,}", "[حذف دادهٔ محرمانه]", text)
    text = re.sub(r"(?:/Users/|/private/|/var/|[A-Z]:\\)[^\s\"']+", "[مسیر خصوصی]", text)
    return text


def _safe_url(value):
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    return value if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password and not parts.query else None


def build_evidence_v3(report: dict) -> ReportEvidenceV3:
    core = numerical_core(report)
    if not isinstance(report.get("job_id"), str) or not report["job_id"]:
        raise EvidenceBuildError("CONTEXT_MISMATCH", "شناسهٔ اجرای منبع موجود نیست.")
    digest = value_hash(core)
    facts: list[FactRecord] = []
    sources: list[EvidenceRecord] = []
    omissions: list[OmissionRecord] = []
    spec = ((report.get("problem_revision") or {}).get("spec") or {})
    objective = spec.get("objective") or {}
    variables = spec.get("variables") or []
    candidates = (report.get("candidate_plan") or {}).get("candidates") or []
    trials = report.get("trials") or []
    ids = {x.get("algorithm_id") or x.get("implementation_id") for x in candidates + trials}
    ids.update(x.get("algorithm_id") for x in (report.get("decision") or {}).get("metrics", []))
    ids.update(x for x in [report.get("selected_algorithm_id"), (report.get("solution_origin") or {}).get("algorithm_id")] if x)
    methods = [MethodIdentity(alias=f"m{i}", implementation_id=name, display_name_fa=method_display_name(name, f"m{i}")) for i, name in enumerate(sorted(x for x in ids if isinstance(x, str)), 1)]
    aliases = {m.implementation_id: m.alias for m in methods}
    labels = {m.implementation_id: m.display_name_fa for m in methods}

    def source(identifier, value, locator):
        text = _clean_text(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        sources.append(EvidenceRecord(evidence_id=identifier, kind="run_record", locator=locator, text=text, content_sha256=sha256(text.encode()).hexdigest(), original_content_sha256=None, excerpt_start=None, excerpt_end=None, source_url=None, source_title=None, scope="run_specific", stale=None))

    def fact(identifier, value, kind, path, refs, *, unit=None, operation=None, inputs=None):
        if isinstance(value, str):
            value = _clean_text(value)
        display = "ثبت نشده" if value is None else format_value(value, unit)
        labels_by_fact = {
            "selection.decisive_criterion": {"median_score":"میانهٔ امتیاز پایلوت", "normalized_iqr":"پراکندگی نرمال‌شده پس از تساوی میانه‌ها", "algorithm_id":"ترتیب شناسه‌ها پس از تساوی امتیاز و پراکندگی", "policy_assignment":"سیاست ثابت ثبت‌شده", "not_reached":"تصمیم‌گیری انجام نشده"},
            "solution.origin_phase": {"main":"اجرای اصلی", "pilot":"اجرای آزمایشی پایلوت", "profile":"نمونه‌برداری مسئله", "preflight":"بررسی اولیه"},
            "solution.status": {"valid_best_found":"بهترین مشاهدهٔ تأییدشده", "no_valid_solution":"جواب معتبر موجود نیست", "validation_failed":"بازبینی ناموفق", "validation_incomplete":"بازبینی کامل نشده"},
            "solution.optimality_status": {"not_certified":"بهینگی سراسری گواهی نشده است"},
            "verification.status": {"passed":"موفق", "failed":"ناموفق", "pending":"در انتظار بررسی", "incomplete":"ناقص", "not_recorded":"ثبت نشده"},
            "proposal.source": {"library_research":"پیشنهاد مدل با استفاده از شواهد کتابخانهٔ محلی", "llm":"پیشنهاد مدل زبانی", "llm_proposal":"پیشنهاد مدل زبانی", "fixed_solver":"سیاست روش ثابت", "rule_based":"سیاست مبتنی بر قواعد"},
        }
        if type(value) is str:display = labels_by_fact.get(identifier, {}).get(value, display)
        if type(value) is str:
            import re
            for implementation, readable in labels.items():
                display = display.replace(implementation, readable)
            display = re.sub(r"mealpy:[A-Za-z0-9_.:-]+", lambda m: method_display_name(m[0]), display)
            display = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{40,64}\b", "شناسهٔ فنی", display, flags=re.I)
            display = re.sub(r"https?://[^\s]+", "منبع ارجاع‌شده", display, flags=re.I)
            display = re.sub(r"\b[a-zA-Z_][\w.-]*\.(?:json|py|sqlite3|jsonl)\b", "فایل ثبت‌شده", display)
        facts.append(FactRecord(fact_id=identifier, value_type=kind, value=value, availability="missing" if value is None else "available", unit=unit, display_text=display, source_paths=[path] if path else [], evidence_refs=list(refs), origin="derived" if operation else "recorded", derivation=DerivationRecord(operation=operation, input_fact_refs=inputs or [], definition_version="1.0") if operation else None))

    def derived_source(identifier, value, inputs):
        text = _clean_text(json.dumps({"value": value, "inputs": inputs}, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        sources.append(EvidenceRecord(evidence_id="derived:"+identifier, kind="derived_calculation", locator="calculation:"+identifier, text=text, content_sha256=sha256(text.encode()).hexdigest(), original_content_sha256=None, excerpt_start=None, excerpt_end=None, source_url=None, source_title=None, scope="run_specific", stale=None))

    expression = (objective.get("source") or {}).get("expression") or (objective.get("source") or {}).get("function_id")
    problem_source = {"expression": expression, "sense": objective.get("sense"), "variables": [{k: v.get(k) for k in ("name", "type", "lower", "upper", "unit")} for v in variables], "parameters": spec.get("parameters"), "deterministic": spec.get("deterministic"), "constraints": spec.get("constraints")}
    source("report:problem", problem_source, "/problem_revision/spec")
    fact("problem.expression", expression, "text", "/problem_revision/spec/objective/source", ["report:problem"])
    fact("problem.sense", {"minimize": "کمینه‌سازی", "maximize": "بیشینه‌سازی"}.get(objective.get("sense")), "text", "/problem_revision/spec/objective/sense", ["report:problem"])
    fact("problem.variable_count", len(variables) if spec else None, "integer", "/problem_revision/spec/variables", ["report:problem"], operation="count" if spec else None)
    domains, domain_refs = [], []
    for i, variable in enumerate(variables, 1):
        prefix = f"variable.v{i}"
        for name in ("name", "lower", "upper"):
            fact(prefix+"."+name, variable.get(name), "text" if name == "name" else "number", f"/problem_revision/spec/variables/{i-1}/{name}", ["report:problem"])
        point = report.get("best_x")
        fact(prefix+".solution_value", point[i-1] if isinstance(point, list) and len(point) >= i else None, "number", f"/best_x/{i-1}", ["report:solution"])
        domain_refs.extend(prefix+"."+name for name in ("name", "lower", "upper"))
        if variable.get("lower") is not None and variable.get("upper") is not None:
            domains.append(f"{variable.get('name')} در بازهٔ [{format_value(variable['lower'])}, {format_value(variable['upper'])}]")
    fact("problem.domain", "؛ ".join(domains) if domains else None, "text", "/problem_revision/spec/variables", ["report:problem"], operation="format_group" if domains else None, inputs=domain_refs)
    if variables:
        facts[:] = [f.model_copy(update={"derivation":DerivationRecord(operation="count",input_fact_refs=[f"variable.v{i}.name" for i in range(1,len(variables)+1)],definition_version="1.0")}) if f.fact_id=="problem.variable_count" else f for f in facts]
    fact("problem.parameters", json.dumps(spec.get("parameters"), ensure_ascii=False) if "parameters" in spec else None, "text", "/problem_revision/spec/parameters", ["report:problem"])
    source("report:proposal", {"source": report.get("proposal_source"), "rationales": [{"method": labels.get(x.get("algorithm_id") or x.get("implementation_id")), "rationale": x.get("rationale_fa"), "evidence_refs": x.get("evidence_refs", [])} for x in candidates], "selection_policy": (report.get("policy") or {}).get("selection_policy")}, "/candidate_plan")
    fact("proposal.source", report.get("proposal_source") or (report.get("effective_selection_policy") if report.get("selected_algorithm_id") else None), "text", "/proposal_source", ["report:proposal"])
    fact("proposal.rationale", "؛ ".join(_clean_text(x.get("rationale_fa") or "دلیل معتبر ثبت نشده") for x in candidates) if candidates else None, "text", "/candidate_plan/candidates", ["report:proposal"])
    for method in methods:
        fact(f"method.{method.alias}.label", method.display_name_fa, "text", "/solver_cards", ["report:proposal"])
    method_fact_refs = [f"method.{aliases.get(x.get('algorithm_id') or x.get('implementation_id'))}.label" for x in candidates if (x.get("algorithm_id") or x.get("implementation_id")) in aliases]
    if not method_fact_refs and report.get("selected_algorithm_id") in aliases:
        method_fact_refs = ["method."+aliases[report["selected_algorithm_id"]]+".label"]
    fact("proposal.methods", "، ".join(labels[x.get("algorithm_id") or x.get("implementation_id")] for x in candidates if (x.get("algorithm_id") or x.get("implementation_id")) in labels) if candidates else labels.get(report.get("selected_algorithm_id")), "text", "/candidate_plan/candidates", ["report:proposal"], operation="format_group" if method_fact_refs else None, inputs=method_fact_refs)

    # Existing policy projection owns eligibility, rank order and the decisive field.
    try:
        summary = build_decision_summary(report)
    except (ValueError, KeyError, TypeError) as error:
        raise EvidenceBuildError("CONTEXT_MISMATCH", "تصمیم ثبت‌شده با نتیجهٔ گزارش سازگار نیست.") from error
    metrics = (report.get("decision") or {}).get("metrics") or []
    metric_by_id = {m["algorithm_id"]: m for m in metrics}
    source("report:decision", {"metrics": metrics, "selected_algorithm_id": report.get("selected_algorithm_id"), "criterion": summary.decisive_criterion if summary else None, "selection_status": report.get("selection_status")}, "/decision")
    source("report:trials", [{"algorithm_id": t.get("algorithm_id"), "phase": t.get("phase"), "replicate_index": t.get("replicate_index"), "execution_status": t.get("execution_status"), "started_evaluations": t.get("started_evaluations"), "objective_raw": (t.get("best") or {}).get("objective_raw"), "score": (t.get("best") or {}).get("score_for_minimization"), "effective_configuration": (t.get("numeric_execution") or {}).get("effective_configuration") or t.get("configuration_snapshot")} for t in trials], "/trials")
    for method in methods:
        prefix = "pilot."+method.alias
        pilots = [t for t in trials if t.get("phase") == "pilot" and t.get("algorithm_id") == method.implementation_id]
        valid = [t for t in pilots if t.get("execution_status") == "completed" and t.get("history_complete") is True and (t.get("best") or {}).get("status") == "successful"]
        counts = [t.get("started_evaluations") for t in pilots]
        metric = metric_by_id.get(method.implementation_id)
        if metric and metric.get("trial_ids") != [t.get("trial_id") for t in sorted(pilots,key=lambda t:t["replicate_index"])]:
            raise EvidenceBuildError("CONTEXT_MISMATCH", "ارجاع Trialهای تصمیم با پایلوت‌های همان اجرا سازگار نیست.")
        scores = [(t.get("best") or {}).get("score_for_minimization") for t in valid]
        raws = [(t.get("best") or {}).get("objective_raw") for t in valid]
        if any(type(x) not in (int, float) for x in scores+raws):
            raise EvidenceBuildError("CONTEXT_MISMATCH", "مقدار پایلوت معتبر نیست.")
        if metric and metric.get("eligible"):
            if not valid or len(valid) != (report.get("policy") or {}).get("pilot_repeats"):
                raise EvidenceBuildError("CONTEXT_MISMATCH", "تعداد پایلوت معتبر با تصمیم واجدشرایط سازگار نیست.")
            recomputed = median(scores)
            scale = (report.get("decision") or {}).get("scale")
            if type(scale) not in (int, float) or scale <= 0:
                raise EvidenceBuildError("CONTEXT_MISMATCH", "مقیاس تصمیم معتبر نیست.")
            scaled = [v / scale for v in scores]
            spread = _quantile(scaled, .75) - _quantile(scaled, .25)
            if recomputed != metric.get("median_score") or not math.isclose(spread, metric.get("normalized_iqr"), rel_tol=1e-12, abs_tol=1e-15):
                raise EvidenceBuildError("CONTEXT_MISMATCH", "میانه یا پراکندگی پایلوت با دادهٔ Trial سازگار نیست.")
        fact(prefix+".objective_raw_values", raws if pilots else None, "number_vector", "/trials", ["report:trials"])
        fact(prefix+".score_values", scores if pilots else None, "number_vector", "/trials", ["report:trials"])
        fact(prefix+".valid_repeats", len(valid) if pilots else None, "integer", "/trials", ["report:trials"], operation="count" if pilots else None, inputs=[prefix+".objective_raw_values"])
        fact(prefix+".evaluation_counts", counts if pilots and all(type(x) is int for x in counts) else None, "number_vector", "/trials", ["report:trials"])
        fact(prefix+".median_score", metric.get("median_score") if metric else None, "number", "/decision/metrics", ["report:decision"])
        fact(prefix+".median_objective_raw", median(raws) if raws else None, "number", "/trials", ["report:trials"], operation="median" if raws else None, inputs=[prefix+".objective_raw_values"])
        fact(prefix+".normalized_iqr", metric.get("normalized_iqr") if metric else None, "number", "/decision/metrics", ["report:decision"])
        fact(prefix+".eligible", metric.get("eligible") if metric else None, "boolean", "/decision/metrics", ["report:decision"])
    chosen = report.get("selected_algorithm_id")
    criterion = summary.decisive_criterion if summary and summary.mode == "empirical_pilots" else "policy_assignment" if summary else "not_reached"
    fact("selection.method_label", labels.get(chosen), "text", "/selected_algorithm_id", ["report:decision"])
    fact("selection.decisive_criterion", criterion, "text", "/decision", ["report:decision"])
    fact("selection.status", report.get("selection_status"), "text", "/selection_status", ["report:decision"])
    ranked = summary.eligible_ranking if summary else []
    runner = ranked[1] if len(ranked) > 1 else None
    fact("selection.runner_up_method_label", labels.get(runner), "text", "/decision/metrics", ["report:decision"])
    if runner and chosen and objective.get("sense") == "minimize":
        win, second = metric_by_id[chosen]["median_score"], metric_by_id[runner]["median_score"]
        if win >= 0 and second > 0:
            inputs = ["pilot."+aliases[chosen]+".median_score", "pilot."+aliases[runner]+".median_score"]
            value = 100 * (second - win) / second
            derived_source("median_relative_decrease", value, inputs)
            fact("selection.relative_decrease_percent", value, "number", "/decision/metrics", ["derived:median_relative_decrease"], operation="relative_decrease_percent", inputs=inputs, unit="percent")

    origin = report.get("solution_origin") or {}
    verified = report.get("solution_status") == "valid_best_found" and (report.get("verification") or {}).get("validation_status") == "passed"
    solution_data = {key: report.get(key) for key in ("best_x", "objective_raw", "solution_origin", "solution_status", "optimality_status", "partial_result", "job_status", "termination_reason", "main_success")}
    source("report:solution", solution_data, "/solution_origin")
    fact("solution.x", report.get("best_x") if verified else None, "number_vector", "/best_x", ["report:solution"])
    fact("solution.objective_raw", report.get("objective_raw") if verified else None, "number", "/objective_raw", ["report:solution"])
    fact("solution.status", report.get("solution_status"), "text", "/solution_status", ["report:solution"])
    fact("solution.origin_phase", origin.get("phase"), "text", "/solution_origin/phase", ["report:solution"])
    fact("solution.origin_method_label", labels.get(origin.get("algorithm_id")) if origin.get("algorithm_id") else {"preflight": "بررسی اولیه", "profile": "نمونه‌برداری مسئله"}.get(origin.get("phase")), "text", "/solution_origin/algorithm_id", ["report:solution"])
    fact("solution.optimality_status", report.get("optimality_status"), "text", "/optimality_status", ["report:solution"])
    fact("execution.job_status", report.get("job_status"), "text", "/job_status", ["report:solution"])
    fact("execution.termination_reason", report.get("termination_reason"), "text", "/termination_reason", ["report:solution"])
    fact("execution.partial_result", report.get("partial_result"), "boolean", "/partial_result", ["report:solution"])
    membership = bool(verified and isinstance(report.get("best_x"), list) and len(report["best_x"]) == len(variables) and all(v["lower"] <= x <= v["upper"] for x, v in zip(report["best_x"], variables))) if verified and variables else None
    fact("solution.within_bounds", membership, "boolean", "/best_x", ["report:solution", "report:problem"], operation="bounds_membership" if membership is not None else None, inputs=["solution.x", "problem.domain"])
    verification = report.get("verification") or {}
    source("report:verification", {"verification": verification, "error": report.get("verification_error"), "optimality_status": report.get("optimality_status")}, "/verification")
    fact("verification.status", verification.get("validation_status"), "text", "/verification/validation_status", ["report:verification"])
    fact("verification.repeat_objective_raw", verification.get("repeat_objective_raw"), "number_vector", "/verification/repeat_objective_raw", ["report:verification"])
    budget = report.get("budget") or {}
    pilot_counts = [t.get("started_evaluations") for t in trials if t.get("phase") == "pilot"]
    main_counts = [t.get("started_evaluations") for t in trials if t.get("phase") == "main"]
    source("report:budget", {"budget": budget, "pilot_started": sum(pilot_counts) if all(type(x) is int for x in pilot_counts) else None, "main_started": sum(main_counts) if all(type(x) is int for x in main_counts) else None, "numeric_compute_seconds": (report.get("timing") or {}).get("elapsed_compute_seconds")}, "/budget")
    fact("budget.total_started", budget.get("known_started"), "integer", "/budget/known_started", ["report:budget"])
    for name, counts in [("pilot", pilot_counts), ("main", main_counts)]:
        fact("budget."+name+"_counts", counts if counts and all(type(x) is int for x in counts) else None,"number_vector","/trials",["report:trials"])
        fact("budget."+name+"_started", sum(counts) if counts and all(type(x) is int for x in counts) else None, "integer", "/trials", ["report:budget", "report:trials"], operation="sum" if counts and all(type(x) is int for x in counts) else None, inputs=["budget."+name+"_counts"])
    fact("timing.numeric_compute_seconds", (report.get("timing") or {}).get("elapsed_compute_seconds"), "number", "/timing/elapsed_compute_seconds", ["report:budget"], unit="seconds")
    source("report:profile", {key: (report.get("profile") or {}).get(key) for key in ("n_variables", "objective_sense", "sample_count", "normalized_iqr", "normalized_std")}, "/profile")
    for name, value in [("zero", 0), ("one", 1), ("two", 2)]:
        facts.append(FactRecord(fact_id="constant."+name, value_type="integer", value=value, availability="available", unit=None, display_text=str(value), source_paths=[], evidence_refs=[], origin="mathematical_constant", derivation=None))

    research = report.get("research") or {}
    passages = {p.get("evidence_id"): p for p in research.get("evidence", [])}
    passage_indices = {p.get("evidence_id"): i for i,p in enumerate(research.get("evidence", []))}
    documents = {p.get("document_id"): p for p in research.get("documents", [])}
    chosen_passages = []
    for candidate in candidates:
        refs = sorted(set(candidate.get("evidence_refs") or []))
        chosen_passages.extend(refs[:2])
    if not candidates:chosen_passages = sorted(passages)[:10]
    for identifier in sorted(passages):
        if identifier not in chosen_passages:omissions.append(OmissionRecord(item_ref="source:"+identifier,reason="not_relevant"))
    chosen_passages = list(dict.fromkeys(chosen_passages))
    for index, identifier in enumerate(chosen_passages):
        passage = passages.get(identifier)
        if not passage:
            omissions.append(OmissionRecord(item_ref="source:"+identifier, reason="missing"))
            continue
        if index >= 10:
            omissions.append(OmissionRecord(item_ref="source:"+identifier, reason="size_limit"))
            continue
        original = passage.get("text")
        if not isinstance(original, str):
            omissions.append(OmissionRecord(item_ref="source:"+identifier, reason="missing"));continue
        original_hash = sha256(original.encode()).hexdigest()
        excerpt = _clean_text(original[:1500])
        # Do not claim original offsets when sanitation would change the excerpt.
        if excerpt != original[:1500]:
            omissions.append(OmissionRecord(item_ref="source:"+identifier, reason="not_relevant"));continue
        document = documents.get(passage.get("document_id"), {})
        url = _safe_url(passage.get("source_url") or passage.get("url") or document.get("final_url"))
        scope = passage.get("evidence_scope") or ("family_level" if passage.get("selection_method") == "family_evidence" else "implementation_level")
        sources.append(EvidenceRecord(evidence_id="source:"+identifier, kind="source_passage", locator=f"/research/evidence/{passage_indices[identifier]}/text[0:{len(excerpt)}]", text=excerpt, content_sha256=sha256(excerpt.encode()).hexdigest(), original_content_sha256=original_hash, excerpt_start=0, excerpt_end=len(excerpt), source_url=url, source_title=_clean_text(document["title"]) if document.get("title") else None, scope=scope, stale=passage.get("stale") if type(passage.get("stale")) is bool else document.get("stale")))

    available = {f.fact_id for f in facts if f.availability == "available"}
    required = {
        "problem": ["problem.expression", "problem.sense", "problem.variable_count", "problem.domain"],
        "candidate_rationale": ["proposal.methods", "proposal.source", "proposal.rationale"],
        "selection": ["selection.method_label", "selection.decisive_criterion"],
        "solution": ["solution.x", "solution.objective_raw", "solution.origin_phase", "solution.origin_method_label"] if verified else ["solution.status"],
        "verification_and_limits": ["verification.status", "budget.total_started", "budget.main_started", "solution.optimality_status"],
        "mathematical_analysis": [],
    }
    if ranked and len(ranked) > 1:
        for method_id in ranked[:2]:
            a = aliases[method_id]
            required["selection"].extend([f"method.{a}.label", f"pilot.{a}.median_score", f"pilot.{a}.median_objective_raw", f"pilot.{a}.valid_repeats", f"pilot.{a}.evaluation_counts"])
    if not chosen:
        required["selection"].append("selection.status")
    source_ids = [x.evidence_id for x in sources]
    sections = [SectionRequirement(section_id=section, required=section != "mathematical_analysis", required_fact_refs=[ref for ref in required[section] if ref in available], allowed_evidence_refs=source_ids, requirement_text_fa={"problem":"مسئله، هدف و دامنهٔ ثبت‌شده را روشن توضیح دهید.","candidate_rationale":"پیشنهاد روش‌ها و دلیل ثبت‌شده یا نبود آن را از انتخاب نهایی جدا کنید.","selection":"معیار واقعی انتخاب و حداقل مقایسهٔ دو روش نخست را با بودجه و تکرار واقعی توضیح دهید؛ شناسه در تساوی کامل برتری عددی نیست.","solution":"جواب تأییدشده و منشأ واقعی آن را بیان کنید؛ نبود جواب را پنهان نکنید.","verification_and_limits":"بازبینی، مصرف اصلی و کل و محدودیت متناسب را جدا توضیح دهید.","mathematical_analysis":"فقط تفسیر نظری مرتبط؛ بررسی نمادین مستقل انجام نشده است."}[section]) for section in SECTION_ORDER]
    result = ReportEvidenceV3(schema_version="3.0", job_id=str(report.get("job_id") or "unknown"), source_report_schema_version=str(report.get("schema_version") or "unknown"), source_core_sha256=digest, facts=facts, sources=sources, sections=sections, methods=methods, omissions=omissions, evidence_policy_version="1.0")
    # Drop complete optional records, never truncate JSON/core strings.
    while len(canonical_bytes(result)) > 65536:
        removable = next((s for s in reversed(result.sources) if s.kind == "source_passage"), None)
        if removable is None:
            raise EvidenceBuildError("CONTEXT_TOO_LARGE", "هستهٔ الزامی شواهد از سقف مجاز بزرگ‌تر است.")
        new_sources = [s for s in result.sources if s.evidence_id != removable.evidence_id]
        new_sections = [s.model_copy(update={"allowed_evidence_refs": [r for r in s.allowed_evidence_refs if r != removable.evidence_id]}) for s in result.sections]
        result = result.model_copy(update={"sources":new_sources,"sections":new_sections,"omissions":[*result.omissions,OmissionRecord(item_ref=removable.evidence_id,reason="size_limit")]})
    return result
