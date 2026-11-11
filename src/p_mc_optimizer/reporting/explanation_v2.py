"""Deterministic Persian explanations rendered from validated run evidence."""
from __future__ import annotations

from p_mc_optimizer.llm.explanation_v2 import (
    ExplanationFactsV1, ExplainerContextV2, ExplainerPlanV2, allowed_explanation_notes,
    default_note_ids, validate_explainer_plan_v2,
)
from p_mc_optimizer.llm.formulation import RoleOutputError
from p_mc_optimizer.llm.models import ExplainerDecisionSummary

_SELECTION = {
    "median_score": "روش {method} به دلیل کمترین میانهٔ امتیاز در پایلوت‌های معتبر انتخاب شد.",
    "normalized_iqr": "پس از برابر شدن میانهٔ امتیاز روش‌های صدر رتبه‌بندی، پراکندگی کمترِ امتیازها باعث انتخاب روش {method} شد.",
    "algorithm_id": "میانهٔ امتیاز و پراکندگی روش‌های صدر رتبه‌بندی برابر بود؛ ترتیب ثابت شناسهٔ الگوریتم، روش {method} را تعیین کرد.",
    "policy_assignment": "روش {method} بر اساس سیاست ثبت‌شدهٔ اجرا تعیین شد؛ مقایسهٔ پایلوت مبنای این انتخاب نبوده است.",
}
_RESULT = {
    "valid_best_found": "بهترین مشاهدهٔ معتبرِ یافت‌شده در محدودهٔ بودجهٔ این اجرا گزارش شده است.",
    "no_valid_solution": "این اجرا جواب معتبرِ تأییدشده‌ای برای گزارش فراهم نکرد.",
    "validation_failed": "نقطهٔ ثبت‌شده با معیار بازبینی عددی سازگار نبود.",
    "validation_incomplete": "بررسی نهایی نقطهٔ مشاهده‌شده کامل نشد.",
}
_ORIGIN = {
    "preflight": "{noun} از مرحلهٔ پیش‌پرواز به دست آمده است.",
    "profile": "{noun} از مرحلهٔ نمونه‌برداری مسئله به دست آمده است.",
    "pilot": "{noun} از اجرای پایلوت روش {origin_method} به دست آمده است.",
    "main": "{noun} از اجرای اصلی روش {origin_method} به دست آمده است.",
}
_VERIFICATION = {
    "passed": "بازمحاسبهٔ تابع در نقطهٔ ثبت‌شده با مقدار ثبت‌شده توافق عددی داشت.",
    "failed": "بازمحاسبهٔ تابع با معیار توافق عددی سازگار نبود.",
    "incomplete": "بازبینی عددی کامل نشده است.",
    "pending": "بازبینی عددی کامل نشده است.",
    "not_applicable": "بازبینی عددی در این وضعیت کاربرد نداشته است.",
    "not_recorded": "نتیجه‌ای از بازبینی عددی ثبت نشده است.",
}
_LIMITATION = "بهینگی سراسری در این اجرا اثبات نشده است."


def render_explanation_v2(
    facts: ExplanationFactsV1 | dict,
    decision_summary: ExplainerDecisionSummary | dict | None,
    note_ids: list[str],
    *,
    method_labels: dict[str, str] | None = None,
) -> dict:
    """Return plain text. HTML and UI consumers use their normal safe text paths."""
    facts = facts if isinstance(facts, ExplanationFactsV1) else ExplanationFactsV1.model_validate(facts)
    decision = (decision_summary.model_dump(mode="json")
                if isinstance(decision_summary, ExplainerDecisionSummary) else decision_summary)
    notes = allowed_explanation_notes(facts, decision)
    if len(set(note_ids)) != len(note_ids) or set(note_ids) - {note.note_id for note in notes}:
        raise RoleOutputError("note_ids: contains an ineligible or duplicate note")
    method_labels = method_labels or {}
    method = method_labels.get(facts.selected_algorithm_id, facts.selected_algorithm_id)
    origin_method = method_labels.get(facts.origin_algorithm_id, facts.origin_algorithm_id)
    reason = facts.selection_reason
    selection = _SELECTION[reason].format(method=method) if reason in _SELECTION else None
    if reason == "median_score" and facts.objective_sense == "maximize":
        selection += " در این مسئلهٔ بیشینه‌سازی، امتیاز مقایسه منفیِ مقدار تابع است و امتیاز کمتر ترجیح دارد."
    sentences = []
    if reason == "insufficient_evidence":
        sentences.append("شواهد پایلوت برای انتخاب روش کافی نبود.")
    elif reason == "not_reached":
        sentences.append("مرحلهٔ انتخاب روش در این اجرا تکمیل نشد.")
    sentences.append(_RESULT[facts.solution_status])
    if facts.origin_phase is not None:
        noun = "این جواب" if facts.solution_status == "valid_best_found" else "نقطهٔ مشاهده‌شده"
        sentences.append(_ORIGIN[facts.origin_phase].format(noun=noun, origin_method=origin_method))
    sentences.append(_VERIFICATION[facts.verification_status])
    limits = [_LIMITATION]
    if decision and decision["mode"] == "empirical_pilots":
        limits.append("رتبه‌بندی پایلوت به بودجه و بذرهای همین اجرا وابسته است و برتری آماری عمومی را اثبات نمی‌کند.")
    if facts.partial_result:
        limits.append("اجرای ثبت‌شده کامل نیست؛ وضعیت توقف و مراحل را بررسی کنید.")
    if facts.proposal_fallback:
        limits.append("در مرحلهٔ پیشنهاد روش، برنامهٔ جایگزینِ ثبت‌شده به کار رفته است.")
    evidence = ["report:solution", "report:verification", "report:budget"]
    if decision is not None:
        evidence.append("report:decision")
    return {"summary_fa": " ".join(sentences), "selection_explanation_fa": selection,
            "limitations_fa": limits, "evidence_refs": evidence}


def render_notes(facts: ExplanationFactsV1, decision_summary: dict | None, note_ids: list[str]) -> list[dict]:
    catalog = {note.note_id: note for note in allowed_explanation_notes(facts, decision_summary)}
    if len(set(note_ids)) != len(note_ids) or set(note_ids) - catalog.keys():
        raise RoleOutputError("note_ids: contains an ineligible or duplicate note")
    return [catalog[note_id].model_dump(mode="json") for note_id in note_ids]


def _method_labels(report: dict) -> dict[str, str]:
    labels = {}
    for card in report.get("solver_cards") or []:
        identifier = card.get("algorithm_id") or card.get("implementation_id")
        configuration = card.get("configuration") or {}
        class_name = configuration.get("class_name")
        class_path = card.get("class_path")
        if identifier and (class_name or class_path):
            labels[identifier] = class_name or class_path.rsplit(".", 1)[-1]
    return labels


def apply_explanation_v2(
    report: dict,
    context: ExplainerContextV2 | dict,
    plan: ExplainerPlanV2 | dict | None = None,
    *,
    llm_schema_id: str | None = None,
) -> None:
    """Add server-rendered explanation fields without changing numerical results."""
    context = context if isinstance(context, ExplainerContextV2) else ExplainerContextV2.model_validate(context)
    if plan is not None:
        plan = plan if isinstance(plan, ExplainerPlanV2) else ExplainerPlanV2.model_validate(plan)
        validate_explainer_plan_v2(plan, context)
        note_ids = list(plan.note_ids)
        source = "llm"
    else:
        note_ids = default_note_ids(context)
        source = "default"
    decision = context.report["decision_summary"]
    report["explanation"] = render_explanation_v2(context.facts, decision, note_ids, method_labels=_method_labels(report))
    report["explanation_notes"] = render_notes(context.facts, decision, note_ids)
    report["explanation_contract_version"] = "2.0"
    report["explanation_evidence"] = {
        "status": "consistent", "facts_schema_version": "1.0", "facts_hash": context.facts_hash,
        "facts": context.facts.model_dump(mode="json"), "renderer_version": "1.0",
        "note_catalog_version": "1.0", "note_selection_source": source, "note_ids": note_ids,
        "llm_schema_id": llm_schema_id,
    }


def apply_invalid_context_explanation_v2(report: dict, *, llm_schema_id: str | None = None) -> None:
    report["explanation_contract_version"] = "2.0"
    report["explanation"] = {
        "summary_fa": "برای توضیح این اجرا، شواهد سازگار و کافی در دسترس نیست. جزئیات ثبت‌شده را در بخش فنی بررسی کنید.",
        "selection_explanation_fa": None, "limitations_fa": [_LIMITATION], "evidence_refs": [],
    }
    report["explanation_notes"] = []
    report["explanation_evidence"] = {
        "status": "invalid_context", "facts_schema_version": "1.0", "facts_hash": None,
        "facts": None, "renderer_version": "1.0", "note_catalog_version": "1.0",
        "note_selection_source": "none", "note_ids": [], "llm_schema_id": llm_schema_id,
    }
