"""Shared, data-only names, numeric formatting and v3 report presentation."""
from __future__ import annotations

from html import escape
import ipaddress
import math
import re
from statistics import median
from urllib.parse import quote, urlsplit

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
        version = markers[0] if markers[0] in {'1.0','1.1','1.2'} else '1.0'
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


def _proposal_details(evidence, report=None):
    """The full recorded text remains inspectable; no server paraphrase or slicing."""
    source = next((s for s in evidence.get('sources',[]) if s['evidence_id']=='report:proposal'), None)
    if not source:
        return []
    import json
    try:
        rows = json.loads(source['text']).get('rationales',[])
    except (ValueError, TypeError, AttributeError):
        return []
    sources = {item['evidence_id']: item for item in evidence.get('sources', [])}
    research = (report or {}).get('research') or {}
    passages = {row.get('evidence_id'): row for row in research.get('evidence') or []}
    documents = {row.get('document_id'): row for row in research.get('documents') or []}
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        refs = list(row.get('evidence_refs') or [])
        references = []
        for ref in refs:
            item = sources.get(ref) or sources.get('source:' + ref)
            if item:
                passage = passages.get(ref.removeprefix('source:')) or {}
                document = documents.get(passage.get('document_id')) or {}
                anchor = passage.get('section')
                anchor = anchor if isinstance(anchor, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]{0,255}', anchor) else None
                url = _safe_url(item.get('source_url'))
                if url and anchor:
                    url = urlsplit(url)._replace(fragment=quote(anchor, safe='._-:')).geturl()
                references.append({'title': _reference_title(ref, item, version='1.1'),
                                   'url': url, 'text': item.get('text'), 'section': anchor,
                                   'start': passage.get('start'), 'end': passage.get('end'),
                                   'document_text_sha256': document.get('text_sha256'),
                                   'locator': item.get('locator'), 'content_sha256': item.get('content_sha256')})
        result.append({'method': row.get('method') or 'نام روش ثبت نشده',
                       'rationale': row.get('rationale'), 'evidence_refs': refs, 'references': references})
    return result


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


def _valid_count(value):
    return value if type(value) is int and value >= 0 else None


def _phase_started(report, phase):
    trials = report.get('trials')
    if not isinstance(trials, list):
        return None
    rows = [row for row in trials if row.get('phase') == phase]
    counts = [_valid_count(row.get('started_evaluations')) for row in rows]
    return sum(counts) if rows and all(value is not None for value in counts) else None


def catalog_selection_view(report):
    """Use this run's recorded shortlist; never consult today's catalog."""
    frozen = report.get('catalog_selection_summary') or {}
    sent = frozen.get('sent_implementation_ids')
    if isinstance(report.get('catalog_selection_summary'), dict):
        if _valid_count(frozen.get('sent_count')) is None:
            sent = None
    elif not isinstance(sent, list):
        # In older v2 reports solver_cards is the persisted advisor shortlist.
        cards = report.get('solver_cards') if report.get('advisor_plan_v2') else None
        sent = ([card['implementation_id'] for card in cards if isinstance(card, dict)
                 and isinstance(card.get('implementation_id'), str)] if isinstance(cards, list) else None)
    if sent is not None:
        sent = list(dict.fromkeys(value for value in sent if isinstance(value, str)))
    plan = report.get('candidate_plan') or {}
    nominated = list(dict.fromkeys(row.get('implementation_id') or row.get('algorithm_id')
                                  for row in plan.get('candidates') or []
                                  if row.get('implementation_id') or row.get('algorithm_id')))
    counts = {name: _valid_count(frozen.get(name)) for name in ('discovered_count', 'qualified_count', 'eligible_count')}
    counts['sent_count'] = _valid_count(frozen.get('sent_count')) if isinstance(report.get('catalog_selection_summary'), dict) else (len(sent) if sent is not None else None)
    counts['nominated_count'] = len(nominated) if isinstance(plan.get('candidates'), list) else None
    return {**counts, 'sent': [{'implementation_id': value, 'label': method_display_name(value)} for value in sent or []],
            'nominated': [{'implementation_id': value, 'label': method_display_name(value)} for value in nominated],
            'not_nominated': [{'implementation_id': value, 'label': method_display_name(value)}
                              for value in sent or [] if value not in nominated],
            'limited_choice': counts['sent_count'] == counts['nominated_count'] and bool(nominated),
            'catalog_hash': frozen.get('catalog_hash')}


def model_usage_view(report):
    """Only sum complete, valid recorded usage. Missing usage/cost stays unknown."""
    calls = report.get('llm_calls')
    if not isinstance(calls, list):
        return {'recorded_calls': None, 'reserved_calls': None, 'unknown_calls': None,
                'input_tokens': None, 'output_tokens': None, 'total_tokens': None,
                'cost': 'ثبت نشده', 'roles': []}

    def total(rows, field, *, provenance=False):
        values = [_valid_count((row.get('provenance') or {}).get(field) if provenance
                               else (row.get('usage') or {}).get(field)) for row in rows]
        return sum(values) if rows and all(value is not None for value in values) else None

    role_labels = {'formulator': 'صورت‌بندی', 'research_planner': 'طرح پژوهش', 'planner': 'طرح پژوهش',
                   'advisor': 'پیشنهاد روش', 'explainer': 'نویسندهٔ گزارش', 'report_reviewer': 'بازبین گزارش'}
    roles = []
    for role in dict.fromkeys(row.get('role') for row in calls):
        rows = [row for row in calls if row.get('role') == role]
        roles.append({'role': role, 'label': role_labels.get(role, 'نقش ثبت‌شده'), 'calls': len(rows),
                      'input_tokens': total(rows, 'prompt_tokens'), 'output_tokens': total(rows, 'completion_tokens'),
                      'request_body_bytes': total(rows, 'request_body_bytes', provenance=True)})
    cost = report.get('cost_estimate')
    if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
        cost_text = format_value(cost) + ' (واحد طبق رکورد هزینه)'
    elif isinstance(cost, dict) and type(cost.get('amount')) in (int, float) and math.isfinite(cost['amount']) and cost['amount'] >= 0:
        cost_text = format_value(cost['amount']) + ' ' + str(cost.get('currency') or 'واحد ثبت نشده')
    else:
        cost_text = 'ثبت نشده'
    return {'recorded_calls': len(calls), 'reserved_calls': sum(row.get('status') == 'reserved' for row in calls),
            'unknown_calls': sum(row.get('status') == 'outcome_unknown' for row in calls),
            'input_tokens': total(calls, 'prompt_tokens'), 'output_tokens': total(calls, 'completion_tokens'),
            'total_tokens': total(calls, 'total_tokens'), 'cost': cost_text, 'roles': roles}


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
    counts = {"budget_allowed": budget.get("max_objective_evaluations"), "total_started": budget.get("known_started"),
              "main_started": _phase_started(report, 'main'), "pilot_started": _phase_started(report, 'pilot'),
              "numeric_compute_seconds": timing.get("elapsed_compute_seconds"), "wall_seconds": timing.get("elapsed_wall_seconds"),
              "llm_seconds": timing.get("elapsed_llm_seconds")}
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
                catalog_selection=catalog_selection_view(report), model_usage=model_usage_view(report),
                narrative_notice=(None if narrative.get('mode') == 'llm' else
                                  'بخشی از متن مدل تأیید شد؛ بخش‌های دیگر با داده‌های ثبت‌شده تکمیل شده‌اند.' if narrative.get('mode') == 'mixed' else
                                  'برای این اجرا متن تأییدشدهٔ مدل در دسترس نیست؛ گزارش خودکار از داده‌های ثبت‌شده نمایش داده می‌شود.'),
                rationale_details=_proposal_details(evidence, report) if version == '1.1' else [],
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
        narrative_issues=narrative.get('issues') or [],
        catalog_selection_summary=report.get('catalog_selection_summary'),
    )
    return view



def render_view_model_html(vm):
    """Trusted layout plus escaped plain text; shared verbatim by Streamlit and export."""
    e = lambda value: escape(str(value if value is not None else "ثبت نشده"), quote=True)
    ltr = lambda value: '<bdi dir="ltr">' + e(value) + '</bdi>'
    out = ['<div class="narrative-v3" dir="rtl">', '<style>.narrative-v3{font-size:16px;line-height:1.8;overflow-wrap:anywhere;max-width:100%;min-width:0}.narrative-v3 p{white-space:pre-wrap}.narrative-v3 section{margin:1rem 0;padding:1rem;border:1px solid #8893a955;border-radius:12px;min-width:0}.narrative-v3 .table-scroll{overflow-x:auto;max-width:100%;overscroll-behavior-x:contain}.narrative-v3 table{border-collapse:collapse;min-width:680px;width:100%}.narrative-v3 th,.narrative-v3 td{padding:.6rem;text-align:start;border-bottom:1px solid #8893a955}.narrative-v3 td bdi{white-space:nowrap}.narrative-v3 bdi[dir=ltr],.narrative-v3 code{direction:ltr;unicode-bidi:isolate;overflow-wrap:normal;word-break:normal}.narrative-v3 a{overflow-wrap:anywhere}.narrative-v3 details{max-width:100%;padding:.75rem;margin-block:.75rem;border-radius:10px}.narrative-v3 summary{cursor:pointer;font-weight:650}.narrative-v3 pre{white-space:pre-wrap;overflow-wrap:anywhere}.narrative-v3 h2{font-size:1.3rem;line-height:1.55;margin:.3rem 0 .8rem}.narrative-v3 h3{font-size:1.1rem;line-height:1.6;margin:.2rem 0 .6rem}.narrative-v3 .math-line{overflow-x:auto;direction:ltr;text-align:left;white-space:nowrap}.narrative-v3 [data-block=progress]{overflow-x:auto;max-width:100%}.narrative-v3 [data-block=progress] figure{min-width:620px}.narrative-v3 svg{font-size:16px}.narrative-v3 svg text{fill:#C7CDDA}.narrative-v3 svg polyline{stroke:#FDA481}.narrative-v3 .source-summary{font-size:.93em}.narrative-v3 .count-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.25rem 1rem}</style>']
    out.append('<section data-block="solution"><h2>جواب و بررسی عددی</h2><p>وضعیت جواب: ' + e(vm['solution_label']) + '</p>')
    if vm.get('read_error'):
        out.append('<p class="narrative-read-error" role="status">' + e(vm['read_error']) + '</p>')
    if vm['verified']:
        out.append('<p>نقطهٔ تأییدشده: ' + ltr(vm['best_x_display']) + '</p><p>مقدار تابع: ' + ltr(vm['objective_display']) + '</p>')
    else:
        out.append('<p>جواب تأییدشده‌ای برای این اجرا ثبت نشده است.</p>')
    out.append('<p>روش منتخب: ' + e(vm['selected_method']) + '</p><p>منشأ جواب: ' + e(vm['origin_phase']) + '، ' + e(vm['origin_method']) + '</p></section>')
    out.append('<details data-block="problem"><summary>مدل مسئله و کران‌ها</summary><p>' + e(vm['problem']['sense']) + '</p><p class="math-line">' + ltr(vm['problem']['expression']) + '</p>')
    for v in vm['problem']['variables']:
        out.append('<p class="math-line">' + ltr(v['lower'] + ' ≤ ' + str(v['name']) + ' ≤ ' + v['upper']) + '</p>')
    out.append('</details>')
    selection = vm['catalog_selection']
    if selection['sent_count'] is not None or selection['nominated_count'] is not None:
        out.append('<section data-block="candidate-selection"><h2>از گزینه‌های مدل تا نامزدهای آزمایش</h2><div class="count-grid">')
        for field, label in [('discovered_count', 'روش‌های کشف‌شده در نصب'), ('qualified_count', 'پیکربندی‌های تأییدشدهٔ محیط'),
                             ('eligible_count', 'روش‌های واجد شرایط مسئله'), ('sent_count', 'کارت‌های واقعاً ارسال‌شده به مدل'),
                             ('nominated_count', 'نامزدهای پیشنهادی')]:
            out.append('<p>' + label + ': ' + ltr(format_value(selection[field])) + '</p>')
        out.append('</div><p class="source-summary">تعدادها از snapshot همین اجرا هستند؛ وضعیت امروز کتابخانه به گزارش نسبت داده نشده است.</p>')
        if selection['nominated']:
            out.append('<p>نامزدهای پیشنهادی: ' + e('، '.join(row['label'] for row in selection['nominated'])) + '</p>')
        if selection['limited_choice']:
            out.append('<p class="choice-limit">تعداد گزینه‌های ارسال‌شده با تعداد نامزدها برابر بوده است؛ این اجرا انتخاب از مجموعه‌ای بزرگ‌تر را نشان نمی‌دهد.</p>')
        if selection['not_nominated']:
            out.append('<details><summary>گزینه‌های ارسال‌شده که نامزد نشدند</summary><p>' + e('، '.join(row['label'] for row in selection['not_nominated'])) + '</p><p>برای این گزینه‌ها در این مقایسه رتبه یا نتیجهٔ عددی ثبت نشده است.</p></details>')
        out.append('</section>')
    if vm['comparison_rows']:
        out.append('<section data-block="comparison"><h2>خلاصهٔ مقایسهٔ پایلوت‌ها</h2><p>رتبه‌بندی با ترتیب میانهٔ امتیاز، IQR نرمال‌شده و شناسهٔ روش برای رفع تساوی انجام شده است.</p><div class="table-scroll" tabindex="0" role="region" aria-label="جدول قابل پیمایش پایلوت‌ها"><table><thead><tr><th>روش</th><th>پایلوت معتبر</th><th>ارزیابی‌های پایلوت</th><th>میانهٔ مقدار تابع</th>' + ('<th>میانهٔ امتیاز کمینه‌سازی</th>' if vm['show_score'] else '') + '<th>IQR نرمال‌شده</th><th>رتبه</th><th>وضعیت</th></tr></thead><tbody>')
        for row in vm['comparison_rows']:
            d = row['display']
            out.append('<tr><th>' + e(row['label']) + '</th><td>' + ltr(d['valid_repeats']) + '</td><td>' + ltr(d['evaluation_counts']) + '</td><td>' + ltr(d['median_objective_raw']) + '</td>' + ('<td>' + ltr(d['median_score']) + '</td>' if vm['show_score'] else '') + '<td>' + ltr(d['normalized_iqr']) + '</td><td>' + ltr(d['rank']) + '</td><td>' + e('واجد شرایط' if row['eligible'] else 'ردشده: ' + str(row['reason'] or 'ثبت نشده')) + '</td></tr>')
        out.append('</tbody></table></div></section>')
    out.append('<h2>' + e(vm['title']) + '</h2>')
    if vm.get('narrative_notice'):
        out.append('<p class="narrative-status" role="status">' + e(vm['narrative_notice']) + '</p>')
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
    labels = {"budget_allowed": "بودجهٔ مجاز ارزیابی", "total_started": "مصرف ثبت‌شدهٔ کل", "main_started": "مصرف اجرای اصلی", "pilot_started": "مصرف پایلوت‌ها", "numeric_compute_seconds": "زمان محاسبات عددی (ثانیه)", "wall_seconds": "زمان کل کار (ثانیه)", "llm_seconds": "زمان تماس مدل (ثانیه)"}
    for key, label in labels.items():
        out.append('<p>' + label + ': ' + ltr(vm['counts_display'][key]) + '</p>')
    out.append('<p>مصرف کل، اجرای اصلی و پایلوت‌ها دامنه‌های جدا دارند. زمان‌ها بازه‌های ثبت‌شده‌اند و ممکن است هم‌پوشانی داشته باشند.</p>')
    usage = vm['model_usage']
    for field, label in [('recorded_calls', 'تماس‌های ثبت‌شدهٔ مدل'), ('input_tokens', 'توکن ورودی'),
                         ('output_tokens', 'توکن خروجی'), ('total_tokens', 'مجموع توکن')]:
        out.append('<p>' + label + ': ' + ltr(format_value(usage[field])) + '</p>')
    out.append('<p>هزینهٔ پولی: ' + e(usage['cost']) + '</p>')
    if usage['reserved_calls'] or usage['unknown_calls']:
        out.append('<p>رزرو فعال تماس: ' + ltr(format_value(usage['reserved_calls'])) + '؛ تماس با نتیجهٔ نامعلوم: ' + ltr(format_value(usage['unknown_calls'])) + '. این رکوردها از حساب مصرف حذف نشده‌اند.</p>')
    if usage['roles']:
        out.append('<details data-block="role-usage"><summary>مصرف و اندازهٔ درخواست هر نقش</summary><div class="table-scroll"><table><thead><tr><th>نقش</th><th>تماس ثبت‌شده</th><th>توکن ورودی</th><th>توکن خروجی</th><th>اندازهٔ درخواست‌ها (بایت)</th></tr></thead><tbody>')
        for role in usage['roles']:
            out.append('<tr><th>' + e(role['label']) + '</th>' + ''.join('<td>' + ltr(format_value(role[field])) + '</td>' for field in ('calls', 'input_tokens', 'output_tokens', 'request_body_bytes')) + '</tr>')
        out.append('</tbody></table></div><p>اگر مصرف یکی از تماس‌ها ثبت نشده باشد، مجموع کامل آن کمیت «ثبت نشده» نمایش داده می‌شود.</p></details>')
    if vm['progress']:
        from p_mc_optimizer.reporting import progress_figure
        out.append('<p class="source-summary">در عرض کم، جدول و نمودار را می‌توانید به‌صورت افقی پیمایش کنید.</p><div data-block="progress" tabindex="0" role="region" aria-label="نمودار قابل پیمایش تاریخچه">' + progress_figure(vm['progress'], number_formatter=format_value) + '</div><details data-block="progress-scale"><summary>نمایش افت‌های کوچک‌تر با محور لگاریتمی متقارن</summary><div data-block="progress" tabindex="0" role="region" aria-label="نمودار قابل پیمایش لگاریتمی">' + progress_figure(vm['progress'], number_formatter=format_value, axis_scale='symlog') + '</div></details><p>برای بیشینه‌سازی، امتیاز کمینه‌سازی برابر منفی مقدار تابع هدف است.</p>')
    else:
        out.append('<p>تاریخچهٔ کافی برای نمودار در این گزارش ثبت نشده است.</p>')
    out.append('</section>')
    if vm.get('show_proposal_details'):
        out.append('<details data-block="proposal-rationales"><summary>دلایل ثبت‌شده پیشنهاد</summary><p>متن کامل دلایلِ رکورد پیشنهاد؛ این قسمت دادهٔ ثبت‌شده است و خلاصهٔ تازهٔ سرور نیست.</p>')
        for item in vm['rationale_details']:
            out.append('<section class="recorded-rationale"><h3>' + e(item['method']) + '</h3><p>' + e(item['rationale'] if isinstance(item['rationale'],str) and item['rationale'].strip() else 'دلیلی برای این نامزد ثبت نشده است.') + '</p></section>')
            for ref in item.get('references') or []:
                out.append('<details class="candidate-evidence"><summary>' + e(ref['title']) + '</summary><blockquote>' + e(ref.get('text')) + '</blockquote>')
                if ref['url']:
                    out.append('<a rel="noopener noreferrer" target="_blank" href="' + e(ref['url']) + '">بازکردن منبع رسمی</a>')
                if ref.get('section'):
                    out.append('<p>بخش منبع: ' + ltr(ref['section']) + '</p>')
                if type(ref.get('start')) is int and type(ref.get('end')) is int:
                    out.append('<p>بازهٔ قطعه در متن ذخیره‌شده: ' + ltr(format_value(ref['start'])) + ' تا ' + ltr(format_value(ref['end'])) + '</p>')
                out.append('<p>محل شاهد: ' + ltr(ref.get('locator')) + '</p><p>هش متن شاهد: ' + ltr(ref.get('content_sha256')) + '</p></details>')
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
