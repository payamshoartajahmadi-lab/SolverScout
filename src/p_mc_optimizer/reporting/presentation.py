"""Shared, data-only names, numeric formatting and v3 report presentation."""
from __future__ import annotations

from html import escape
import ipaddress
import math
import re
from statistics import median
from urllib.parse import urlsplit

SECTION_ORDER = ("problem", "candidate_rationale", "selection", "solution", "verification_and_limits", "mathematical_analysis")
SECTION_TITLES = dict(zip(SECTION_ORDER, ("مسئله و فرض‌ها", "چرا این روش‌ها بررسی شدند؟", "مقایسه و دلیل انتخاب", "جواب و تفسیر آن", "بررسی جواب و حدود نتیجه", "تحلیل ریاضی تکمیلی مدل")))
MODE_TITLES = {"llm": "گزارش تحلیلی مدل", "mixed": "گزارش تحلیلی با تکمیل خودکار", "deterministic": "گزارش خودکار بر پایهٔ داده‌های اجرا"}
_FAMILIES = {"de": "تکامل تفاضلی (DE)", "gwo": "گرگ خاکستری (GWO)", "woa": "بهینه‌سازی نهنگ (WOA)", "abc": "کلونی زنبور مصنوعی (ABC)", "pso": "ازدحام ذرات (PSO)", "cma_es": "سازگاری ماتریس کوواریانس (CMA-ES)"}
TERMINATION_LABELS = {
    "evaluation_budget": "رسیدن به سقف ارزیابی‌های مجاز",
    "time_budget": "رسیدن به سقف زمان مجاز",
    "solver_stop": "توقف ثبت‌شدهٔ حل‌گر",
    "execution_error": "خطای اجرای عددی",
    "validation_error": "ناموفق‌بودن اعتبارسنجی",
    "insufficient_evidence": "کافی‌نبودن شواهد مقایسه",
    "llm_budget": "رسیدن به سقف تماس‌های مدل",
    "cancelled": "لغو اجرا",
}


def format_value(value, unit=None):
    if value is None:
        return "ثبت نشده"
    if isinstance(value, bool):
        return "بله" if value else "خیر"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(item, unit) for item in value) + "]"
    if type(value) in (int, float) and unit in {"percent", "%", "درصد", "seconds", "second", "s", "ثانیه"}:
        if not math.isfinite(value):
            raise ValueError("Nonfinite report value")
        return format(0 if value == 0 else value, ".1f" if unit in {"percent", "%", "درصد"} else ".3g")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Nonfinite report value")
        if unit in {"percent", "%", "درصد"}:
            return format(value, ".1f")
        if unit in {"seconds", "second", "s", "ثانیه"}:
            return format(value, ".3g")
        return "0" if value == 0 else format(value, ".8g")
    return str(value)


def format_full_value(value):
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_full_value(item) for item in value) + "]"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Nonfinite report value")
        return format(value, ".17g")
    return format_value(value)


def format_fact_value(fact_id, value, unit=None, *, version="1.0"):
    """Composable fact text. Historical formatting is deliberately unchanged."""
    if version not in {"1.0", "1.1"}:
        raise ValueError("Unsupported narrative formatter version")
    if version == "1.0" or value is None:
        return format_value(value, unit)
    if fact_id == "solution.optimality_status":
        return "گواهی‌نشده" if value == "not_certified" else "وضعیت بهینگی با نگاشت نمایشی نامشخص"
    if fact_id == "solution.within_bounds":
        if type(value) is not bool:
            raise ValueError("Bounds membership requires an actual boolean")
        return "داخل کران‌های ثبت‌شده" if value else "خارج از کران‌های ثبت‌شده"
    if fact_id == "execution.termination_reason":
        return TERMINATION_LABELS.get(value, "علت توقف با نگاشت نمایشی نامشخص")
    if fact_id == "solution.origin_phase":
        return {"main": "اجرای اصلی", "pilot": "اجرای آزمایشی پایلوت", "profile": "نمونه‌برداری مسئله", "preflight": "بررسی اولیه"}.get(value, "مرحله با نگاشت نمایشی نامشخص")
    if fact_id.endswith(".evaluations_per_repeat"):
        if type(value) is not int or value < 0:
            raise ValueError("Common evaluation count requires a nonnegative integer")
        return format_value(value) + " ارزیابی"
    if unit in {"percent", "%", "درصد"}:
        if type(value) not in (int, float):
            raise ValueError("Percentage requires a real numerical value")
        return format_value(value, "percent") + " درصد"
    return format_value(value, unit)


def method_display_name(identifier, alias=None):
    if not identifier:
        return "ثبت نشده"
    if identifier.startswith("mealpy:"):
        parts = identifier.split(":")
        module, name = parts[-2:] if len(parts) >= 3 else ("", "")
        family = module.rsplit(".", 1)[-1].lower()
    else:
        family, _, name = identifier.partition(".")
    if name == "CMA_ES":
        return _FAMILIES["cma_es"]
    known = _FAMILIES.get(family.lower())
    if known and (not name or name.startswith("Original") or name in {"original", "mealpy", "CMA_ES"}):
        return known
    readable = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]{0,79}", readable or ""):
        return f"{known} · {readable}" if known else readable
    suffix = f" ({alias})" if re.fullmatch(r"m[1-9][0-9]*", alias or "") else ""
    return "روش ثبت‌شده" + suffix


def _dump(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _safe_url(url):
    if not isinstance(url, str):
        return None
    try:
        p = urlsplit(url)
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.hostname.lower() in {"localhost", "localhost.localdomain"}:
            return None
        try:
            if not ipaddress.ip_address(p.hostname).is_global:
                return None
        except ValueError:
            if "." not in p.hostname:
                return None
        return url
    except ValueError:
        return None


def comparison_rows(report):
    decision = report.get("decision") or {}
    if decision.get("decision_source") != "empirical_pilot_selection":
        return []
    metrics = decision.get("metrics") or []
    from p_mc_optimizer.llm.explanation_context import build_decision_summary
    summary = build_decision_summary(report)
    metric_by_id = {m['algorithm_id']: m for m in metrics}
    eligible = [metric_by_id[identifier] for identifier in summary.eligible_ranking] if summary else []
    ranking = {m["algorithm_id"]: i + 1 for i, m in enumerate(eligible)}
    ordered = eligible + sorted((m for m in metrics if not m.get("eligible")), key=lambda m: m["algorithm_id"])
    rows = []
    for m in ordered:
        identifier = m["algorithm_id"]
        trials = [t for t in report.get("trials") or [] if t.get("phase") == "pilot" and t.get("algorithm_id") == identifier]
        selected_ids = set(m.get("trial_ids") or [])
        valid = [t for t in trials if t.get("trial_id") in selected_ids and t.get("execution_status") == "completed"]
        raw = [(t.get("best") or {}).get("objective_raw", t.get("objective_raw")) for t in valid]
        raw = [v for v in raw if type(v) in (int, float) and math.isfinite(v)]
        counts = [t.get("started_evaluations") for t in trials]
        raw_median = median(raw) if raw else None
        rows.append({"implementation_id": identifier, "label": method_display_name(identifier), "valid_repeats": len(valid), "evaluation_counts": counts, "median_objective_raw": raw_median, "median_score": m.get("median_score"), "normalized_iqr": m.get("normalized_iqr"), "rank": ranking.get(identifier), "eligible": m.get("eligible") is True, "reason": m.get("reason"), "display": {"valid_repeats": format_value(len(valid)), "evaluation_counts": format_value(counts), "median_objective_raw": format_value(raw_median), "median_score": format_value(m.get("median_score")), "normalized_iqr": format_value(m.get("normalized_iqr")), "rank": format_value(ranking.get(identifier))}, "full_precision": {"median_objective_raw": format_full_value(raw_median), "median_score": format_full_value(m.get("median_score")), "normalized_iqr": format_full_value(m.get("normalized_iqr"))}})
    return rows


def _read_narrative(report):
    """Reopen frozen raw sections; an unknown version never selects a new builder."""
    from p_mc_optimizer.llm.narrative_models import NarrativeV3, validate_narrative_version_combination
    original = report.get('narrative_v3') or {}
    markers = []
    if 'report_generation_policy_version' in report:
        markers.append(report['report_generation_policy_version'])
    if 'execution_contracts' in report:
        markers.append((report.get('execution_contracts') or {}).get('report_generation_policy_version', '1.0'))
    if not markers:
        markers = ['1.0']
    try:
        value = NarrativeV3.model_validate(original).model_dump(mode='json')
        for marker in markers:
            validate_narrative_version_combination((value.get('evidence') or {}).get('evidence_policy_version'),
                                                 value['evidence_builder_version'], value['formatter_version'], marker)
        # Reopening is a pure verification of the persisted projection. Never
        # attach reviewed prose to changed facts, another Job, or a stale core.
        from p_mc_optimizer.reporting.evidence_v3 import core_hash, evidence_hash
        evidence = value.get('evidence')
        if evidence is not None:
            if (value['evidence_status'] != 'valid'
                    or evidence_hash(evidence) != value['context_hash']
                    or evidence['source_core_sha256'] != value['source_core_sha256']
                    or value['source_core_sha256'] != core_hash(report)
                    or evidence['job_id'] != str(report.get('job_id') or 'unknown')
                    or evidence['source_report_schema_version'] != str(report.get('schema_version') or 'unknown')):
                raise ValueError('Narrative evidence is not bound to this persisted report')
        elif (value['evidence_status'] != 'invalid' or value['context_hash'] is not None
              or value['source_core_sha256'] is not None or value['mode'] != 'deterministic'
              or any(s['origin'] == 'llm' for s in value['accepted_sections'])):
            raise ValueError('Reviewed model prose requires its frozen evidence')
        return value, None
    except (ValueError, KeyError, TypeError):
        from p_mc_optimizer.reporting.narrative_v3 import finalize_narrative
        version = markers[0] if markers[0] in {'1.0','1.1'} else '1.0'
        fallback = finalize_narrative(report, None, [], requested=False, evidence_invalid=True,
                                     report_generation_policy_version=version)
        return fallback, 'نسخه، شواهد یا پیوند داده‌های این گزارش قابل بازسازی معتبر نیست؛ متن جایگزین سرور نمایش داده می‌شود و متن مدل دوباره تولید نشد.'


def _reference_title(ref, source, *, version):
    if version == '1.0':
        return source.get('source_title') or {'run_record':'دادهٔ همین اجرا','derived_calculation':'محاسبه از دادهٔ همین اجرا'}.get(source.get('kind'),'منبع ثبت‌شده')
    labels = {'report:problem':'مدل و دامنهٔ مسئله', 'report:proposal':'پیشنهاد و دلایل نامزدها',
              'report:decision':'معیار و تصمیم انتخاب', 'report:trials':'رکورد اجرای پایلوت‌ها',
              'report:solution':'رکورد جواب و منشأ آن', 'report:verification':'بازبینی عددی جواب',
              'report:budget':'حساب ارزیابی‌های اجرا', 'report:profile':'نمونه‌برداری ثبت‌شده',
              'report:timing':'زمان‌های ثبت‌شده', 'report:execution':'وضعیت و توقف اجرا'}
    if ref in labels:
        return labels[ref]
    if source.get('kind') == 'derived_calculation':
        return 'محاسبهٔ کاهش نسبی میانه' if 'relative_decrease_percent' in ref else 'محاسبه از داده‌های همین اجرا'
    title = source.get('source_title')
    if title:
        for family, label in _FAMILIES.items():
            if re.search(r'(?:\.|\b)' + re.escape(family) + r'(?:\b|_)', title, re.I):
                return 'سند مرجع ' + label
        return title
    return 'سند مرجع ثبت‌شده' if source.get('source_url') else 'شاهد ثبت‌شدهٔ این اجرا'


def _proposal_details(evidence):
    """The full recorded text remains inspectable; no server paraphrase or slicing."""
    source = next((s for s in evidence.get('sources',[]) if s['evidence_id']=='report:proposal'), None)
    if not source:
        return []
    import json
    try:
        rows = json.loads(source['text']).get('rationales',[])
    except (ValueError, TypeError, AttributeError):
        return []
    return [{'method':row.get('method') or 'نام روش ثبت نشده',
             'rationale':row.get('rationale'), 'evidence_refs':list(row.get('evidence_refs') or [])}
            for row in rows if isinstance(row,dict)]


def _effective_output_limits(report):
    limits=[]
    for call in report.get('llm_calls') or []:
        provenance=call.get('provenance') or {}
        role=call.get('role');schema=provenance.get('schema_id',call.get('schema_id'))
        maximum=call.get('effective_max_output_tokens',provenance.get('effective_max_output_tokens'))
        if role not in {'explainer','report_reviewer'} or type(maximum) is not int:
            continue
        limits.append({'role':role,'schema_id':schema,'effective_max_output_tokens':maximum,
                       'invocation_id':call.get('invocation_id'),
                       'report_generation_policy_version':call.get('report_generation_policy_version',provenance.get('report_generation_policy_version')),
                       'report_generation_policy_hash':call.get('report_generation_policy_hash',provenance.get('report_generation_policy_hash'))})
    return limits


def build_report_view_model(report):
    """No inference/API/evaluator calls: reopen the same persisted narrative."""
    narrative, read_error = _read_narrative(report)
    version = narrative['formatter_version']
    evidence = narrative.get("evidence") or {}
    facts = {f["fact_id"]: f for f in evidence.get("facts", [])}
    accepted = {item["section"]["section_id"]: item for item in narrative.get("accepted_sections", [])}
    stored = {s["section_id"]: s for s in narrative.get("rendered_sections", [])}
    # Render from accepted raw text plus immutable evidence instead of trusting a modified resolved string.
    if evidence and accepted:
        from p_mc_optimizer.llm.narrative_models import ReportEvidenceV3, NarrativeSectionV3
        from p_mc_optimizer.reporting.narrative_validation import render_sections
        try:
            rebuilt = render_sections(ReportEvidenceV3.model_validate(evidence), [NarrativeSectionV3.model_validate(item["section"]) for item in accepted.values()])
            stored = {s["section_id"]: s for s in [_dump(value) for value in rebuilt]}
        except (ValueError, KeyError, TypeError):
            stored = {s: value for s, value in stored.items() if accepted.get(s, {}).get("origin") == "server"}
    source_by_id = {s["evidence_id"]: s for s in evidence.get("sources", [])}
    sections = []
    for sid in SECTION_ORDER:
        item, section = accepted.get(sid), stored.get(sid)
        if not item or not section:
            continue
        paragraphs = []
        displayed_refs = set()
        raw_paragraphs = {p["paragraph_id"]: p for p in item["section"]["paragraphs"]}
        for p in section["paragraphs"]:
            raw = raw_paragraphs.get(p["paragraph_id"], {})
            references = []
            for ref in raw.get("evidence_refs", []):
                if version == '1.1' and ref in displayed_refs:
                    continue
                source = source_by_id.get(ref)
                if not source:
                    continue
                displayed_refs.add(ref)
                references.append({"evidence_id": ref, "title": _reference_title(ref, source, version=version), "url": _safe_url(source.get("source_url"))})
            segments = []
            raw_text = raw.get("text_fa", "")
            position = 0
            for match in re.finditer(r"\{\{fact:([^{}]+)\}\}", raw_text):
                segments.append({"text": raw_text[position:match.start()], "ltr": False})
                fact = facts.get(match[1])
                if fact:
                    segments.append({"text": fact["display_text"], "ltr": fact["value_type"] in {"number", "integer", "number_vector"} or match[1] == "problem.expression" or (match[1].startswith("variable.") and match[1].endswith(".name"))})
                else:
                    segments.append({"text": match[0], "ltr": False})
                position = match.end()
            segments.append({"text": raw_text[position:], "ltr": False})
            # Rendered server text with no fact context retains its exact persisted plain text.
            if "".join(part["text"] for part in segments) != p["text_fa"]:
                segments = [{"text": p["text_fa"], "ltr": False}]
            paragraphs.append({"paragraph_id": p["paragraph_id"], "text_fa": p["text_fa"], "segments": segments, "claim_kind": raw.get("claim_kind"), "references": references})
        sections.append({"section_id": sid, "title": SECTION_TITLES[sid], "origin": item["origin"], "reviewed": item["origin"] == "llm" and bool(item.get("reviewer_invocation_id")), "interpretation_label": "تحلیل تکمیلی مدل؛ بررسی نمادین مستقل انجام نشده است" if sid == "mathematical_analysis" else None, "paragraphs": paragraphs})
    spec = (report.get("problem_revision") or {}).get("spec") or {}
    objective = spec.get("objective") or {}
    source = objective.get("source") or {}
    verified = (report.get("solution_status") == "valid_best_found" and isinstance(report.get("best_x"), list) and bool(report["best_x"]) and all(type(x) in (int,float) and math.isfinite(x) for x in report["best_x"]) and type(report.get("objective_raw")) in (int,float) and math.isfinite(report["objective_raw"]))
    budget = report.get("budget") or {}; timing = report.get("timing") or {}
    main_trials = [trial for trial in report.get("trials") or [] if trial.get("phase") == "main"]
    main_started = (sum(trial["started_evaluations"] for trial in main_trials)
                    if isinstance(report.get("trials"), list) and all(type(trial.get("started_evaluations")) is int for trial in main_trials) else None)
    counts = {"budget_allowed": budget.get("max_objective_evaluations"), "total_started": budget.get("known_started"), "main_started": main_started, "numeric_compute_seconds": timing.get("elapsed_compute_seconds"), "wall_seconds": timing.get("elapsed_wall_seconds"), "llm_seconds": timing.get("elapsed_llm_seconds")}
    # A numeric whitelist keeps persisted/model strings out of the trusted SVG.
    # This is the existing FE history, never another evaluation or interpolation.
    progress = [{"started_evaluations": point["started_evaluations"],
                 "score_for_minimization": point["score_for_minimization"]}
                for point in report.get("progress") or []
                if isinstance(point, dict)
                and type(point.get("started_evaluations")) is int
                and point["started_evaluations"] >= 0
                and type(point.get("score_for_minimization")) in (int, float)
                and math.isfinite(point["score_for_minimization"])]
    view = {"schema_version": "report-view/3.0", "title": MODE_TITLES.get(narrative.get("mode"), MODE_TITLES["deterministic"]), "mode": narrative.get("mode"), "job_status": report.get("job_status"), "solution_status": report.get("solution_status"), "solution_label": {"valid_best_found":"جواب معتبر پیدا شد", "no_valid_solution":"جواب معتبر پیدا نشد", "validation_failed":"بازبینی جواب ناموفق بود", "validation_incomplete":"بازبینی جواب کامل نشد", "pending":"بررسی در حال انجام است"}.get(report.get("solution_status"),"ثبت نشده"), "verified": verified, "best_x": report.get("best_x") if verified else None, "objective_raw": report.get("objective_raw") if verified else None, "best_x_display": format_value(report.get("best_x")) if verified else "ثبت نشده", "objective_display": format_value(report.get("objective_raw")) if verified else "ثبت نشده", "objective_full": format_full_value(report.get("objective_raw")) if verified else "ثبت نشده", "selected_method": method_display_name(report.get("selected_algorithm_id")), "origin_method": method_display_name((report.get("solution_origin") or {}).get("algorithm_id")), "origin_phase": {"main": "اجرای اصلی", "pilot": "پایلوت", "profile": "نمونه‌برداری", "preflight": "بررسی مقدماتی"}.get((report.get("solution_origin") or {}).get("phase"), "ثبت نشده"), "problem": {"expression": source.get("expression") or source.get("function_id"), "sense": {"minimize": "کمینه‌سازی", "maximize": "بیشینه‌سازی"}.get(objective.get("sense"), "ثبت نشده"), "variables": [{"name": v.get("name"), "lower": format_value(v.get("lower")), "upper": format_value(v.get("upper"))} for v in spec.get("variables", [])]}, "comparison_rows": comparison_rows(report) if narrative.get("evidence_status") == "valid" else [], "show_score": objective.get("sense") == "maximize", "sections": sections, "progress": progress, "counts": counts, "counts_display": {key: format_value(value, "seconds" if key.endswith("seconds") else None) for key, value in counts.items()}, "omitted_sections": narrative.get("omitted_sections") or [], "technical": {"job_id": report.get("job_id"), "context_hash": narrative.get("context_hash"), "source_core_sha256": narrative.get("source_core_sha256"), "attempts": narrative.get("attempts") or [], "methods": evidence.get("methods") or [], "comparison_full_precision": [{"implementation_id": row["implementation_id"], **row["full_precision"]} for row in comparison_rows(report)] if narrative.get("evidence_status") == "valid" else [], "solution_full_precision": format_full_value(report.get("best_x")) if verified else None}}

    view.update(formatter_version=version, read_error=read_error,
                rationale_details=_proposal_details(evidence) if version == '1.1' else [],
                show_proposal_details=version == '1.1' and any(s['evidence_id']=='report:proposal' for s in evidence.get('sources',[])))
    if version == '1.1':
        view['origin_phase'] = facts.get('solution.origin_phase',{}).get('display_text','ثبت نشده')
    view['technical'].update(
        effective_output_limits=_effective_output_limits(report),
        narrative_versions={'formatter_version':version,'evidence_builder_version':narrative['evidence_builder_version'],
                            'evidence_policy_version':evidence.get('evidence_policy_version'),
                            'report_generation_policy_version':report.get('report_generation_policy_version')},
        recorded_narrative_status={key:(report.get('narrative_v3') or {}).get(key) for key in ('mode','review_status','evidence_status')},
        fact_values=[{'fact_id':f['fact_id'],'value':f['value'],'unit':f.get('unit'),'availability':f['availability']} for f in evidence.get('facts',[])],
        read_error=read_error,
    )
    return view



def render_view_model_html(vm):
    """Trusted layout plus escaped plain text; shared verbatim by Streamlit and export."""
    e = lambda value: escape(str(value if value is not None else "ثبت نشده"), quote=True)
    ltr = lambda value: '<bdi dir="ltr">' + e(value) + '</bdi>'
    out = ['<div class="narrative-v3" dir="rtl">', '<style>.narrative-v3{font-size:16px;line-height:1.8;overflow-wrap:anywhere;max-width:100%;min-width:0}.narrative-v3 p{white-space:pre-wrap}.narrative-v3 section{margin:1rem 0;padding:1rem;border:1px solid #8893a955;border-radius:12px;min-width:0}.narrative-v3 .table-scroll{overflow-x:auto;max-width:100%}.narrative-v3 table{border-collapse:collapse;min-width:680px;width:100%}.narrative-v3 th,.narrative-v3 td{padding:.6rem;text-align:start;border-bottom:1px solid #8893a955}.narrative-v3 bdi[dir=ltr],.narrative-v3 code{direction:ltr;unicode-bidi:isolate}.narrative-v3 a{overflow-wrap:anywhere}.narrative-v3 details{max-width:100%}.narrative-v3 pre{white-space:pre-wrap;overflow-wrap:anywhere}.narrative-v3 h2,.narrative-v3 h3{overflow-wrap:anywhere}.narrative-v3 [data-block=progress]{overflow-x:auto;max-width:100%}.narrative-v3 [data-block=progress] figure{min-width:735px}.narrative-v3 svg{font-size:16px}.narrative-v3 svg text{fill:#C7CDDA}.narrative-v3 svg polyline{stroke:#FDA481}</style>']
    out.append('<section data-block="solution"><h2>جواب و بررسی عددی</h2><p>وضعیت جواب: ' + e(vm['solution_label']) + '</p>')
    if vm.get('read_error'):
        out.append('<p class="narrative-read-error" role="status">' + e(vm['read_error']) + '</p>')
    if vm['verified']:
        out.append('<p>نقطهٔ تأییدشده: ' + ltr(vm['best_x_display']) + '</p><p>مقدار تابع: ' + ltr(vm['objective_display']) + '</p>')
    else:
        out.append('<p>جواب تأییدشده‌ای برای این اجرا ثبت نشده است.</p>')
    out.append('<p>روش منتخب: ' + e(vm['selected_method']) + '</p><p>منشأ جواب: ' + e(vm['origin_phase']) + '، ' + e(vm['origin_method']) + '</p></section>')
    out.append('<section data-block="problem"><h2>مدل مسئله و کران‌ها</h2><p>' + e(vm['problem']['sense']) + '</p><p>' + ltr(vm['problem']['expression']) + '</p>')
    for v in vm['problem']['variables']:
        out.append('<p>' + ltr(v['name']) + ': ' + ltr(v['lower']) + ' تا ' + ltr(v['upper']) + '</p>')
    out.append('</section>')
    if vm['comparison_rows']:
        out.append('<section data-block="comparison"><h2>خلاصهٔ مقایسهٔ پایلوت‌ها</h2><div class="table-scroll"><table><thead><tr><th>روش</th><th>پایلوت معتبر</th><th>ارزیابی‌های پایلوت</th><th>میانهٔ مقدار تابع</th>' + ('<th>میانهٔ امتیاز کمینه‌سازی</th>' if vm['show_score'] else '') + '<th>IQR نرمال‌شده</th><th>رتبه</th><th>وضعیت</th></tr></thead><tbody>')
        for row in vm['comparison_rows']:
            d = row['display']
            out.append('<tr><th>' + e(row['label']) + '</th><td>' + ltr(d['valid_repeats']) + '</td><td>' + ltr(d['evaluation_counts']) + '</td><td>' + ltr(d['median_objective_raw']) + '</td>' + ('<td>' + ltr(d['median_score']) + '</td>' if vm['show_score'] else '') + '<td>' + ltr(d['normalized_iqr']) + '</td><td>' + ltr(d['rank']) + '</td><td>' + e('واجد شرایط' if row['eligible'] else 'ردشده: ' + str(row['reason'] or 'ثبت نشده')) + '</td></tr>')
        out.append('</tbody></table></div></section>')
    out.append('<h2>' + e(vm['title']) + '</h2>')
    for section in vm['sections']:
        out.append('<section data-section="' + e(section['section_id']) + '"><h3>' + e(section['title']) + '</h3>')
        if section['origin'] == 'server' and vm['mode'] == 'mixed':
            out.append('<p class="section-origin">تکمیل خودکار بر پایهٔ داده‌های اجرا</p>')
        if section['reviewed']:
            out.append('<p class="section-review">بازبینی خودکار متن انجام شد.</p>')
        if section['interpretation_label']:
            out.append('<p>' + e(section['interpretation_label']) + '</p>')
        for p in section['paragraphs']:
            out.append('<p data-paragraph="' + e(p['paragraph_id']) + '">' + "".join(ltr(segment["text"]) if segment["ltr"] else e(segment["text"]) for segment in p["segments"]) + '</p>')
            if p['references']:
                out.append('<div class="citations">')
                for ref in p['references']:
                    out.append(('<a rel="noopener noreferrer" target="_blank" href="' + e(ref['url']) + '">' + e(ref['title']) + '</a>') if ref['url'] else '<span>' + e(ref['title']) + '</span>')
                    out.append(' ')
                out.append('</div>')
        out.append('</section>')
    out.append('<p class="general-limit">بهینگی سراسری در این اجرا اثبات نشده است.</p><section data-block="usage"><h2>مصرف و زمان‌ها</h2>')
    labels = {"budget_allowed": "بودجهٔ مجاز ارزیابی", "total_started": "مصرف ثبت‌شدهٔ کل", "main_started": "مصرف اجرای اصلی", "numeric_compute_seconds": "زمان محاسبات عددی (ثانیه)", "wall_seconds": "زمان کل کار (ثانیه)", "llm_seconds": "زمان تماس مدل (ثانیه)"}
    for key, label in labels.items():
        out.append('<p>' + label + ': ' + ltr(vm['counts_display'][key]) + '</p>')
    if vm['progress']:
        from p_mc_optimizer.reporting import progress_figure
        out.append('<div data-block="progress">' + progress_figure(vm['progress'], number_formatter=format_value) + '</div>')
    out.append('</section>')
    if vm.get('show_proposal_details'):
        out.append('<details data-block="proposal-rationales"><summary>دلایل ثبت‌شده پیشنهاد</summary><p>متن کامل دلایلِ رکورد پیشنهاد؛ این قسمت دادهٔ ثبت‌شده است و خلاصهٔ تازهٔ سرور نیست.</p>')
        for item in vm['rationale_details']:
            out.append('<section class="recorded-rationale"><h3>' + e(item['method']) + '</h3><p>' + e(item['rationale'] if isinstance(item['rationale'],str) and item['rationale'].strip() else 'دلیلی برای این نامزد ثبت نشده است.') + '</p></section>')
        if not vm['rationale_details']:
            out.append('<p>در رکورد پیشنهاد، نامزد یا دلیل تفصیلی ثبت نشده است.</p>')
        out.append('</details>')
    out.append('<details><summary>جزئیات فنی و نمایش کامل اعداد</summary><p>مقدار تابع با دقت کامل: ' + ltr(vm['objective_full']) + '</p>')
    if vm.get('formatter_version') == '1.1':
        shown=set()
        for item in vm['technical']['effective_output_limits']:
            key=(item['role'],item['effective_max_output_tokens'])
            if key in shown:
                continue
            shown.add(key)
            label='نویسنده' if item['role']=='explainer' else 'بازبین'
            out.append('<p class="effective-output-limit">سقف مؤثر خروجی ' + label + ': ' + ltr(format_value(item['effective_max_output_tokens'])) + ' توکن</p>')
        if not shown:
            out.append('<p>سقف مؤثر خروجی نویسنده و بازبین در دادهٔ تماس‌های این گزارش ثبت نشده است.</p>')
    out.append('<pre dir="ltr">')
    import json
    out.append(e(json.dumps(vm['technical'], ensure_ascii=False, indent=2)))
    out.append('</pre></details></div>')
    return ''.join(out)
