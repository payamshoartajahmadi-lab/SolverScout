"""Evidence-bound explanation contracts and the server-owned educational notes."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from p_mc_optimizer.domain.models import ContractModel, Sha256Hex
from p_mc_optimizer.llm.explanation_context import build_explainer_context
from p_mc_optimizer.llm.formulation import RoleOutputError
from p_mc_optimizer.llm.models import ExplainerDecisionSummary, Repair

SelectionReason = Literal[
    "median_score", "normalized_iqr", "algorithm_id", "policy_assignment",
    "insufficient_evidence", "not_reached",
]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


class ExplanationFactsV1(ContractModel):
    schema_version: Literal["1.0"]
    selection_reason: SelectionReason
    selected_algorithm_id: str | None
    decision_source: str | None
    effective_selection_policy: str | None
    objective_sense: Literal["minimize", "maximize"] | None
    score_semantics: Literal["raw_objective", "negated_objective"] | None
    solution_status: Literal["valid_best_found", "no_valid_solution", "validation_failed", "validation_incomplete"]
    origin_phase: Literal["preflight", "profile", "pilot", "main"] | None
    origin_algorithm_id: str | None
    verification_status: Literal["passed", "failed", "incomplete", "pending", "not_applicable", "not_recorded"]
    optimality_status: Literal["not_certified"]
    partial_result: bool
    proposal_fallback: bool

    @model_validator(mode="after")
    def consistent_facts(self) -> Self:
        selected = self.selection_reason not in {"not_reached", "insufficient_evidence"}
        if selected != (self.selected_algorithm_id is not None):
            raise ValueError("The selection reason and selected method disagree")
        if self.selection_reason == "not_reached":
            if self.decision_source is not None or self.effective_selection_policy is not None:
                raise ValueError("An unreached selection cannot have a decision source or policy")
        elif not self.decision_source or not self.effective_selection_policy:
            raise ValueError("A recorded decision requires its source and effective policy")
        expected_score = {None: None, "minimize": "raw_objective", "maximize": "negated_objective"}[self.objective_sense]
        if self.score_semantics != expected_score:
            raise ValueError("Objective direction and score semantics disagree")
        algorithm_origin = self.origin_phase in {"pilot", "main"}
        if algorithm_origin != (self.origin_algorithm_id is not None):
            raise ValueError("The observation origin and origin method disagree")
        if self.solution_status == "valid_best_found" and (self.origin_phase is None or self.verification_status != "passed"):
            raise ValueError("A valid best observation requires an origin and passed verification")
        if self.solution_status == "validation_failed" and self.verification_status != "failed":
            raise ValueError("A validation failure requires failed verification")
        return self


class ExplanationNote(ContractModel):
    note_id: str
    text_fa: str
    evidence_refs: list[str]


class ExplainerContextV2(ContractModel):
    report: dict[str, Any]
    facts: ExplanationFactsV1
    facts_hash: Sha256Hex
    note_catalog_version: Literal["1.0"]
    allowed_notes: list[ExplanationNote]
    evidence_refs: list[str]
    selection_made: bool
    repair: Repair | None = None


class ExplainerPlanV2(ContractModel):
    schema_version: Literal["2.0"]
    facts: ExplanationFactsV1
    facts_hash: Sha256Hex
    note_ids: list[str] = Field(max_length=3)
    evidence_refs: list[str]


def explanation_facts_hash(facts: ExplanationFactsV1 | dict) -> str:
    values = facts.model_dump(mode="json") if isinstance(facts, ExplanationFactsV1) else facts
    return sha256(canonical_json(values)).hexdigest()


_NOTES = (
    ("median_meaning", "میانه، عملکرد میانی تکرارهای پایلوت را نشان می‌دهد؛ رتبهٔ بهتر در این اجرا به معنای برتری همیشگی روش نیست.", ("report:decision",)),
    ("spread_tiebreak", "وقتی میانه‌ها برابرند، پراکندگی کمترِ امتیازها معیار بعدی رتبه‌بندی است.", ("report:decision",)),
    ("identifier_tiebreak", "ترتیب شناسه‌ها قاعده‌ای ثابت برای شکستن تساوی است و برتری عددی یک روش را نشان نمی‌دهد.", ("report:decision",)),
    ("maximization_scores", "برای مقایسهٔ روش‌ها در مسئلهٔ بیشینه‌سازی، امتیاز داخلی از منفیِ مقدار تابع ساخته می‌شود؛ امتیاز کمتر در این مقایسه بهتر است.", ("report:decision",)),
    ("limited_pilots", "نتایج پایلوت به مسئله، بذرها و بودجهٔ همین اجرا مربوط‌اند و برتری آماری عمومی را اثبات نمی‌کنند.", ("report:decision", "report:budget")),
    ("bounded_search", "جواب گزارش‌شده بهترین مشاهدهٔ معتبر در محدودهٔ بودجه است؛ جست‌وجو با بودجه یا بذر متفاوت ممکن است نتیجهٔ دیگری بدهد.", ("report:solution", "report:budget")),
    ("verification_meaning", "بازبینی عددی، سازگاری مقدار تابع در نقطهٔ ثبت‌شده را بررسی می‌کند و بهینگی سراسری را اثبات نمی‌کند.", ("report:verification",)),
    ("origin_differs", "روش انتخاب‌شده برای اجرای اصلی ممکن است با روشی که بهترین مشاهده را ساخته متفاوت باشد؛ منشأ جواب جداگانه ثبت شده است.", ("report:solution", "report:decision")),
    ("policy_assignment", "در این اجرا، سیاست ثبت‌شده مبنای تعیین روش بوده است؛ از این انتخاب نمی‌توان برتری حاصل از مقایسهٔ پایلوت را نتیجه گرفت.", ("report:decision",)),
    ("insufficient_pilots", "تعداد روش‌های دارای پایلوت‌های معتبر برای تصمیم‌گیری کافی نبود.", ("report:decision",)),
    ("partial_result", "نتیجهٔ این اجرا کامل نیست؛ علت توقف و وضعیت مراحل در جزئیات گزارش آمده‌اند.", ("report:solution",)),
)


def allowed_explanation_notes(facts: ExplanationFactsV1, decision_summary: dict | None) -> list[ExplanationNote]:
    empirical = bool(decision_summary and decision_summary["mode"] == "empirical_pilots" and decision_summary["metrics"])
    applicable = {
        "median_meaning": facts.selection_reason == "median_score",
        "spread_tiebreak": facts.selection_reason == "normalized_iqr",
        "identifier_tiebreak": facts.selection_reason == "algorithm_id",
        "maximization_scores": facts.objective_sense == "maximize" and empirical,
        "limited_pilots": empirical,
        "bounded_search": facts.solution_status == "valid_best_found",
        "verification_meaning": facts.verification_status == "passed",
        "origin_differs": bool(facts.selected_algorithm_id and facts.origin_algorithm_id and facts.selected_algorithm_id != facts.origin_algorithm_id),
        "policy_assignment": facts.selection_reason == "policy_assignment",
        "insufficient_pilots": facts.selection_reason == "insufficient_evidence",
        "partial_result": facts.partial_result,
    }
    return [ExplanationNote(note_id=name, text_fa=text, evidence_refs=list(refs))
            for name, text, refs in _NOTES if applicable[name]]


def build_explainer_context_v2(report: dict) -> dict:
    base = build_explainer_context(report)
    decision = base["report"]["decision_summary"]
    try:
        status = report["selection_status"]
        if decision is None:
            if status != "not_reached" or report.get("selected_algorithm_id") is not None:
                raise RoleOutputError("context: selection has no coherent recorded decision")
            reason = "not_reached"
        elif decision["mode"] == "policy_assignment":
            reason = "policy_assignment"
        elif status == "insufficient_evidence":
            reason = "insufficient_evidence"
        else:
            reason = decision["decisive_criterion"]
        origin = report.get("solution_origin")
        verification = report.get("verification") or {}
        spec = (report.get("problem_revision") or {}).get("spec")
        sense = spec["objective"]["sense"] if spec is not None else None
        facts = ExplanationFactsV1(
            schema_version="1.0", selection_reason=reason,
            selected_algorithm_id=report["selected_algorithm_id"],
            decision_source=decision["decision_source"] if decision else None,
            effective_selection_policy=decision["effective_selection_policy"] if decision else None,
            objective_sense=sense,
            score_semantics={None: None, "minimize": "raw_objective", "maximize": "negated_objective"}[sense],
            solution_status=report["solution_status"],
            origin_phase=origin["phase"] if origin else None,
            origin_algorithm_id=origin["algorithm_id"] if origin else None,
            verification_status=verification.get("validation_status") or verification.get("status") or "not_recorded",
            optimality_status=report["optimality_status"], partial_result=report["partial_result"],
            proposal_fallback=report.get("proposal_source") == "deterministic_fallback",
        )
        if facts.solution_status == "valid_best_found" and (report.get("best_x") is None or report.get("objective_raw") is None):
            raise RoleOutputError("context: a valid best observation requires its point and objective")
        context = ExplainerContextV2(
            **base, facts=facts, facts_hash=explanation_facts_hash(facts), note_catalog_version="1.0",
            allowed_notes=allowed_explanation_notes(facts, decision),
        )
    except ValidationError as error:
        raise RoleOutputError(*(f"context: {'.'.join(map(str, item['loc']))} {item['msg']}"
                                for item in error.errors(include_input=False, include_url=False))) from None
    except (KeyError, TypeError):
        raise RoleOutputError("context: explanation facts are incomplete or inconsistent") from None
    return context.model_dump(mode="json", exclude={"repair"})


def validate_explainer_plan_v2(plan: ExplainerPlanV2, context: ExplainerContextV2) -> None:
    errors = []
    actual, expected = plan.facts.model_dump(mode="json"), context.facts.model_dump(mode="json")
    for key, value in expected.items():
        if actual[key] != value:
            errors.append(f"facts.{key}: differs from recorded facts")
    if plan.facts_hash != context.facts_hash or explanation_facts_hash(plan.facts) != context.facts_hash:
        errors.append("facts_hash: differs from recorded facts hash")
    notes = plan.note_ids
    allowed = {note.note_id for note in context.allowed_notes}
    if len(notes) != len(set(notes)):
        errors.append("note_ids: contains duplicate notes")
    if set(notes) - allowed:
        errors.append("note_ids: contains an ineligible note")
    if not ((1 <= len(notes) <= 3) if allowed else notes == []):
        errors.append("note_ids: expected one to three eligible notes, or none when unavailable")
    if len(plan.evidence_refs) != len(set(plan.evidence_refs)) or set(plan.evidence_refs) != set(context.evidence_refs):
        errors.append("evidence_refs: must contain exactly the supplied evidence")
    if errors:
        raise RoleOutputError(*errors)


def default_note_ids(context: ExplainerContextV2 | dict) -> list[str]:
    value = context if isinstance(context, ExplainerContextV2) else ExplainerContextV2.model_validate(context)
    return [note.note_id for note in value.allowed_notes[:2]]
