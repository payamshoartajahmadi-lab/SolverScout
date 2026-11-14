"""Pure assembly and deterministic fallback for grounded narrative v3."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from time import monotonic

from p_mc_optimizer.llm.narrative_models import (
    AcceptedSectionV3, NarrativeParagraphV3, NarrativeSectionV3, NarrativeV3,
    SECTION_ORDER, REQUIRED_SECTIONS,
)
from p_mc_optimizer.reporting.presentation import format_value, format_fact_value, method_display_name


def _dump(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _section(section_id, text, refs=(), evidence_refs=()):
    if len(text) > 6500:
        text = text[:6400] + "… متن کامل داده‌های ثبت‌شده در بخش مدل و جزئیات گزارش موجود است."
    pieces = []
    while text:
        cut = min(2200, len(text))
        if cut < len(text):
            start = text.rfind("{{fact:", 0, cut)
            if start > text.rfind("}}", 0, cut):
                cut = start
            else:
                space = text.rfind(" ", 0, cut)
                if space > cut // 2:
                    cut = space + 1
        pieces.append(text[:cut]); text = text[cut:]
    import re
    return NarrativeSectionV3(section_id=section_id, paragraphs=[NarrativeParagraphV3(
        paragraph_id="server_" + section_id + ("_"+str(index) if index else ""), claim_kind="recorded", text_fa=piece,
        fact_refs=list(dict.fromkeys(re.findall(r"\{\{fact:([^{}]+)\}\}", piece))), evidence_refs=list(dict.fromkeys(evidence_refs)),
    ) for index, piece in enumerate(pieces or ["ثبت نشده"])])


def _safe_value(value):
    try:
        return format_value(value)
    except (ValueError, TypeError, OverflowError):
        return "ثبت نشده"


def _raw_fallback(report, sid, *, version="1.0"):
    """Only independently recorded display data; no comparison on invalid Evidence."""
    spec = (report.get("problem_revision") or {}).get("spec") or {}
    objective = spec.get("objective") or {}; src = objective.get("source") or {}
    verified = report.get("solution_status") == "valid_best_found" and report.get("best_x") is not None
    if sid == "problem":
        expression = src.get("expression") or src.get("function_id")
        bounds = "؛ ".join(f"{v.get('name', 'متغیر')}: {_safe_value(v.get('lower'))} تا {_safe_value(v.get('upper'))}" for v in spec.get("variables") or [])
        return _section(sid, f"مسئلهٔ ثبت‌شده: {expression or 'فرمول ثبت نشده'}. دامنهٔ متغیرها: {bounds or 'ثبت نشده'}. دادهٔ موجود در این بخش جایگزین اطلاعات غایب نمی‌شود.")
    if sid == "candidate_rationale":
        names = [method_display_name(c.get("implementation_id") or c.get("algorithm_id")) for c in (report.get("candidate_plan") or {}).get("candidates", [])]
        return _section(sid, "نامزدهای ثبت‌شده: " + ("، ".join(names) if names else "ثبت نشده") + ". شواهد سازگارِ کافی برای توضیح علت پیشنهاد در این بسته موجود نیست.")
    if sid == "selection":
        name = method_display_name(report.get("selected_algorithm_id"))
        return _section(sid, f"روش منتخب ثبت‌شده {name} است. به دلیل ناکافی یا ناسازگار بودن شواهد توضیح، ادعای مقایسه‌ای دربارهٔ برتری روش‌ها تولید نشد.")
    if sid == "solution":
        if verified:
            origin = report.get("solution_origin") or {}
            phase = (format_fact_value("solution.origin_phase", origin.get("phase"), version="1.1") if version == "1.1"
                     else {"main": "اجرای اصلی", "pilot": "پایلوت", "profile": "نمونه‌برداری", "preflight": "بررسی مقدماتی"}.get(origin.get("phase"), "ثبت نشده"))
            if version == "1.1" and not origin.get("algorithm_id"):
                return _section(sid, f"نقطهٔ تأییدشده {_safe_value(report.get('best_x'))} با مقدار تابع {_safe_value(report.get('objective_raw'))} ثبت شده است. این نقطه در {phase} ثبت شده؛ نام روش تولیدکننده در رکورد منشأ موجود نیست.")
            return _section(sid, f"نقطهٔ تأییدشده {_safe_value(report.get('best_x'))} با مقدار تابع {_safe_value(report.get('objective_raw'))} ثبت شده است. منشأ نقطه {phase} از {method_display_name(origin.get('algorithm_id'))} است.")
        return _section(sid, "جواب تأییدشده برای این اجرا موجود نیست. وضعیت ثبت‌شدهٔ جواب و علت توقف در داده‌های فنی حفظ شده‌اند.")
    budget = report.get("budget") or {}; verification = report.get("verification") or {}
    label = {"passed": "موفق", "failed": "ناموفق", "incomplete": "ناتمام"}.get(verification.get("validation_status"), "ثبت نشده")
    return _section(sid, f"وضعیت بازبینی عددی {label} است و مصرف ثبت‌شدهٔ کل {_safe_value(budget.get('known_started'))} ارزیابی است. شواهد این بسته برای توضیح بیشتر کافی نیستند؛ هیچ نتیجهٔ غایبی ساخته نشده است.")


def fallback_sections(report, evidence, section_ids, *, report_generation_policy_version=None):
    """Construct useful server paragraphs, retaining all existing required facts."""
    evidence = _dump(evidence)
    from p_mc_optimizer.llm.narrative_policy import freeze_narrative_policy
    generation_version = report_generation_policy_version or (evidence or {}).get("evidence_policy_version", "1.0")
    version = freeze_narrative_policy(generation_version)["formatter_version"]
    if evidence is None:
        return [_raw_fallback(report, sid, version=version) for sid in SECTION_ORDER if sid in section_ids and sid != "mathematical_analysis"]
    facts = {item["fact_id"]: item for item in evidence["facts"]}
    requirements = {item["section_id"]: item for item in evidence["sections"]}
    out = []
    for sid in SECTION_ORDER:
        if sid not in section_ids or sid == "mathematical_analysis":
            continue
        refs = []
        def token(fid):
            if fid not in facts or facts[fid]["availability"] != "available":
                return "ثبت نشده"
            refs.append(fid)
            return "{{fact:" + fid + "}}"
        if sid == "problem":
            text = f"مسئلهٔ ثبت‌شده برای {token('problem.sense')}، تابع {token('problem.expression')} را با {token('problem.variable_count')} متغیر بررسی می‌کند. دامنهٔ واقعی متغیرها {token('problem.domain')} است. پارامترهای ثبت‌شده: {token('problem.parameters')}."
        elif sid == "candidate_rationale":
            if version == "1.1":
                text = f"نامزدهای ثبت‌شده {token('proposal.methods')} هستند. منشأ پیشنهاد ثبت‌شده: {token('proposal.source')}. دلایل اصلی هر نامزد، یا نبود دلیل برای آن، در بخش «دلایل ثبت‌شده پیشنهاد» قابل مشاهده است. پیشنهاد نامزدها از معیار انتخاب نهایی در مقایسهٔ عددی جداست."
            else:
                text = f"نامزدهای ثبت‌شده {token('proposal.methods')} هستند. منشأ پیشنهاد آن‌ها {token('proposal.source')} و دلیل ثبت‌شده {token('proposal.rationale')} است. پیشنهاد نامزدها با نتیجهٔ مقایسهٔ عددی یکسان نیست."
        elif sid == "selection":
            reason = (report.get("decision") or {}).get("decision_source")
            criterion = next((f for f in evidence['facts'] if f['fact_id'] == 'selection.decisive_criterion'), {}).get('value')
            if reason == "empirical_pilot_selection" and criterion not in {"not_reached", "insufficient_evidence", None}:
                winner = token('selection.method_label')
                text = f"روش منتخب ثبت‌شده {winner} است؛ معیار تعیین‌کننده {token('selection.decisive_criterion')} بوده است. "
                if criterion == 'algorithm_id':
                    text += "امتیازها و پراکندگی برابر بودند؛ ترتیب شناسه‌ها فقط رفع تساوی است و برتری عددی را نشان نمی‌دهد. "
                elif criterion == 'normalized_iqr':
                    text += "میانه‌ها برابر بودند و پراکندگی نرمال‌شده ترتیب را تعیین کرد. "
                else:
                    text += "ترتیب میانه‌های ثبت‌شده مبنای انتخاب بود. "
                text += f"روش بعدی {token('selection.runner_up_method_label')} است. "
            elif reason:
                text = f"روش {token('selection.method_label')} طبق سیاست ثبت‌شده تعیین شده است؛ مقایسهٔ تجربیِ ساخته‌شده‌ای به این انتخاب نسبت داده نمی‌شود. "
            else:
                text = "تصمیم معتبرِ روش برای توضیح مقایسه‌ای ثبت نشده است. "
        elif sid == "solution":
            if report.get('solution_status') == 'valid_best_found':
                text = f"نقطهٔ تأییدشده {token('solution.x')} و مقدار اصلی تابع هدف {token('solution.objective_raw')} است. "
                if facts.get('solution.origin_method_label',{}).get('availability') == 'available':
                    text += f"این نقطه در فاز {token('solution.origin_phase')} توسط {token('solution.origin_method_label')} تولید شده است. روش تولیدکنندهٔ نقطه و روش منتخب برای اجرای اصلی دو واقعیت مستقل‌اند."
                else:
                    text += f"این نقطه در فاز {token('solution.origin_phase')} تولید شده است؛ نام روش تولیدکننده در رکورد منشأ ثبت نشده است."
            else:
                text = "جواب تأییدشده‌ای برای انتشار به‌عنوان نتیجهٔ موفق ثبت نشده است. وضعیت ثبت‌شدهٔ جواب و اطلاعات بازبینی در گزارش حفظ شده است."
        else:
            text = f"وضعیت بازبینی عددی {token('verification.status')} و مقادیر بازبینی {token('verification.repeat_objective_raw')} است. مصرف ثبت‌شدهٔ کل {token('budget.total_started')} ارزیابی و مصرف اجرای اصلی {token('budget.main_started')} ارزیابی است؛ این دو کمیت یکسان نیستند. نتیجه به همین مسئله، بذرها، بودجه و تکرارهای ثبت‌شده محدود است."
        # Cover the EvidenceBuilder's exact conditional contract without inventing absent quantities.
        remaining = [fid for fid in requirements.get(sid, {}).get('required_fact_refs', []) if fid not in refs and facts.get(fid, {}).get('availability') == 'available']
        if remaining:
            labels = {
                'selection.winner_median_score': 'میانهٔ امتیاز روش منتخب', 'selection.runner_up_median_score': 'میانهٔ امتیاز روش بعدی',
                'selection.runner_up_median_objective_raw': 'میانهٔ مقدار تابع روش بعدی', 'selection.winner_median_objective_raw': 'میانهٔ مقدار تابع روش منتخب',
            }
            text += ' داده‌های ثبت‌شدهٔ مرتبط: ' + '؛ '.join(labels.get(fid, _fact_label(fid, version=version)) + ' ' + token(fid) for fid in remaining) + '.'
        evidence_refs = []
        allowed = set(requirements.get(sid, {}).get('allowed_evidence_refs') or [])
        for fid in refs:
            evidence_refs.extend(ref for ref in facts[fid]['evidence_refs'] if ref in allowed)
        if not evidence_refs:
            evidence_refs = [ref for ref in requirements.get(sid, {}).get('allowed_evidence_refs') or [] if ref.startswith('report:')][:1]
        out.append(_section(sid, text, refs, evidence_refs))
    return out


def _fact_label(fid, *, version="1.0"):
    if fid.startswith('pilot.'):
        suffix = fid.rsplit('.', 1)[-1]
        return {'valid_repeats':'تعداد پایلوت معتبر', 'evaluation_counts':'ارزیابی‌های واقعی پایلوت', 'evaluations_per_repeat':'ارزیابی آغازشده در هر پایلوت', 'median_score':'میانهٔ امتیاز', 'median_objective_raw':'میانهٔ مقدار اصلی تابع', 'normalized_iqr':'پراکندگی نرمال‌شده'}.get(suffix, 'دادهٔ ثبت‌شده')
    if version == "1.1" and fid in {'solution.optimality_status', 'execution.termination_reason'}:
        return {'solution.optimality_status':'وضعیت بهینگی', 'execution.termination_reason':'علت توقف ثبت‌شده'}[fid]
    return {'solution.status':'وضعیت جواب', 'selection.status':'وضعیت انتخاب', 'verification.status':'وضعیت بازبینی', 'budget.pilot_started':'مصرف پایلوت‌ها'}.get(fid, 'دادهٔ ثبت‌شده')


def finalize_narrative(report, evidence, accepted_sections, *, issues=None, attempts=None,
                       omitted_sections=None, elapsed_reporting_seconds=0.0,
                       provider_failure=False, requested=True, evidence_invalid=False, review_ran=None,
                       report_generation_policy_version=None):
    from p_mc_optimizer.reporting.evidence_v3 import evidence_hash
    from p_mc_optimizer.reporting.narrative_validation import render_sections
    from p_mc_optimizer.llm.narrative_models import validate_narrative_version_combination
    accepted = [_dump(value) for value in accepted_sections]
    issues = [_dump(value) for value in issues or []]
    evidence_dict = _dump(evidence)
    from p_mc_optimizer.llm.narrative_policy import freeze_narrative_policy
    generation_version = report_generation_policy_version or (evidence_dict or {}).get('evidence_policy_version', '1.0')
    try:
        frozen_policy = freeze_narrative_policy(generation_version)
        version = frozen_policy['formatter_version']
        builder_version = frozen_policy['evidence_builder_version']
        validate_narrative_version_combination((evidence_dict or {}).get('evidence_policy_version'), builder_version, version,
                                             report_generation_policy_version)
    except ValueError:
        evidence_invalid = True
        version = report_generation_policy_version if report_generation_policy_version in {'1.0','1.1'} else '1.0'
        builder_version = version
        issues.append({'section_id':None,'paragraph_id':None,'code':'CONTEXT_MISMATCH',
                       'message_fa':'نسخهٔ سیاست گزارش با نسخهٔ شواهد یا قالب نمایش سازگار نیست؛ متن قبلی منتشر نشد.',
                       'evidence_refs':[],'required_change_fa':'با همان نسخه و شواهد ثابت معتبر بازسازی شود؛ ارتقای خاموش مجاز نیست.'})
    if evidence_invalid:
        evidence = None
    if evidence is None or not requested:
        accepted = []
    attempts = [_dump(value) for value in attempts or []]
    evidence_dict = _dump(evidence)
    if review_ran is None:
        review_ran = any(value.get('role') == 'report_reviewer' for value in attempts)
    # Only explicitly reviewed model sections are publishable.
    kept = {item['section']['section_id']: item for item in accepted if item.get('origin') == 'llm' and item.get('reviewer_invocation_id') and item.get('source_draft_hash')}
    model_required = set(kept) & set(REQUIRED_SECTIONS)
    omitted = set(omitted_sections or [])
    if not model_required:
        if 'mathematical_analysis' in kept:
            del kept['mathematical_analysis']
        omitted.add('mathematical_analysis')
    elif 'mathematical_analysis' not in kept:
        omitted.add('mathematical_analysis')
    missing = [sid for sid in REQUIRED_SECTIONS if sid not in kept]
    for section in fallback_sections(report, evidence, missing, report_generation_policy_version=version):
        kept[section.section_id] = {'section':section.model_dump(mode='json'),'origin':'server','source_draft_hash':None,'reviewer_invocation_id':None}
    mode = 'llm' if len(model_required) == len(REQUIRED_SECTIONS) else 'mixed' if model_required else 'deterministic'
    rendered = []
    for sid in SECTION_ORDER:
        if sid not in kept:
            continue
        section = NarrativeSectionV3.model_validate(kept[sid]['section'])
        if evidence is not None:
            rendered.extend(_dump(value) for value in render_sections(evidence, [section]))
        else:
            rendered.append({'section_id':sid,'paragraphs':[{'paragraph_id':p.paragraph_id,'text_fa':p.text_fa} for p in section.paragraphs]})
    payload = {
        'schema_version':'3.0','mode':mode,'evidence_status':'invalid' if evidence_invalid or evidence is None else 'valid',
        'context_hash':evidence_hash(evidence) if evidence is not None else None,
        'source_core_sha256':evidence_dict['source_core_sha256'] if evidence_dict is not None else None,'evidence':evidence_dict,
        'accepted_sections':[kept[sid] for sid in SECTION_ORDER if sid in kept], 'rendered_sections':rendered,
        'review_status':'passed' if mode == 'llm' else 'partial' if mode == 'mixed' else 'failed' if review_ran else 'not_run',
        'omitted_sections':[sid for sid in SECTION_ORDER if sid in omitted],'issues':issues,'attempts':attempts,
        'renderer_version':'3.0.0','formatter_version':version,'evidence_builder_version':builder_version,
        'elapsed_reporting_seconds':max(0.0,float(elapsed_reporting_seconds)),
    }
    # Status reasons are projection inputs, not additions to the closed persisted DTO.
    result = NarrativeV3.model_validate(payload).model_dump(mode='json')
    return result


def apply_narrative_v3(report, payload):
    """Return an additive report projection; numerical fields remain untouched."""
    payload = NarrativeV3.model_validate(payload).model_dump(mode='json')
    result = deepcopy(report)
    result['narrative_v3'] = payload
    if payload['formatter_version'] == '1.1' or 'report_generation_policy_version' in result:
        from p_mc_optimizer.llm.narrative_models import validate_narrative_version_combination
        generation = result.get('report_generation_policy_version') or (result.get('execution_contracts') or {}).get('report_generation_policy_version')
        try:
            validate_narrative_version_combination((payload.get('evidence') or {}).get('evidence_policy_version'),
                                                 payload['evidence_builder_version'], payload['formatter_version'], generation)
        except ValueError:
            generation = None
        result['report_generation_policy_version'] = generation or payload['formatter_version']
    result['explanation_contract_version'] = '3.0'
    sections = {s['section_id']: '\n\n'.join(p['text_fa'] for p in s['paragraphs']) for s in payload['rendered_sections']}
    refs = sorted({ref for item in payload['accepted_sections'] for p in item['section']['paragraphs'] for ref in p['evidence_refs']})
    result['explanation'] = {'summary_fa':sections.get('solution',''), 'selection_explanation_fa':sections.get('selection'), 'limitations_fa':['بهینگی سراسری در این اجرا اثبات نشده است.'], 'evidence_refs':refs}
    result['explanation_notes'] = []
    result['explanation_evidence'] = {'schema_version':'3.0','context_hash':payload['context_hash'],'source_core_sha256':payload['source_core_sha256'],'evidence_status':payload['evidence_status'],'review_status':payload['review_status']}
    requested = (report.get('policy') or {}).get('explain_via_llm') is True and report.get('job_status') == 'completed'
    provider_codes = {'PROVIDER_CONFIGURATION_REQUIRED','PROVIDER_AUTH_FAILED','PROVIDER_UNREACHABLE','PROVIDER_TIMEOUT','PROVIDER_SERVER_ERROR','PROVIDER_RATE_LIMITED','LLM_OUTCOME_UNKNOWN'}
    codes = {attempt['error_code'] for attempt in payload['attempts']}
    if payload['mode'] == 'llm':
        status = 'generated'
    elif not requested:
        status = 'not_requested'
    elif payload['mode'] == 'deterministic' and (codes & provider_codes or report.get('llm_profile') is None and not payload['attempts']):
        status = 'provider_error'
    else:
        status = 'schema_fallback'
    result['explanation_status'] = status
    return result
