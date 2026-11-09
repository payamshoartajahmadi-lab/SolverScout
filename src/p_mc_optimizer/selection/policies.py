"""Policy proposal, stable seeds, and evidence-only pilot ranking."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import uuid4

import numpy as np

from p_mc_optimizer.domain.models import (
    LLM_POLICIES,
    PILOT_EVALUATIONS,
    PILOT_REPEATS,
    SOLVER_CONFIG_IDS,
    Candidate,
    CandidatePlan,
    DomainError,
    ExecutionPolicy,
    ExecutionResult,
    PilotMetric,
    ProblemRevision,
    SelectionDecision,
    TrialRequest,
)
from p_mc_optimizer.profiling.stages import rng, stable_median


TIE_BREAK_ORDER = ["median_score", "normalized_iqr", "algorithm_id"]


@dataclass(frozen=True)
class SelectionContext:
    policy: ExecutionPolicy
    revision: ProblemRevision
    candidate_plan: CandidatePlan | None = None
    proposal_source: str | None = None


class SelectionPolicy:
    """Coordinate the six v1 policies using only the current job's evidence."""

    def propose(self, context: SelectionContext) -> CandidatePlan:
        policy_id = context.policy.selection_policy
        if policy_id not in LLM_POLICIES:
            return propose_baseline(context.policy, context.revision)
        if context.proposal_source == "deterministic_fallback":
            return fallback_plan(policy_id)
        if context.candidate_plan is None:
            raise DomainError("PLAN_VALIDATION_ERROR", "پیشنهاد مشاور در context ثبت نشده است.")
        return validate_plan(context.candidate_plan, count=context.policy.pilot_candidate_count or 1)

    def decide(
        self,
        pilot_summaries: list[tuple[TrialRequest, ExecutionResult]],
        context: SelectionContext,
    ) -> SelectionDecision:
        count = context.policy.pilot_candidate_count
        plan = context.candidate_plan or self.propose(context)
        validate_plan(plan, count=count or 1)
        if count:
            return rank_pilots(pilot_summaries, plan)
        source = context.proposal_source or context.policy.selection_policy
        return fixed_decision(plan, source)


def derive_seed(
    master_seed: int,
    problem_math_hash: str,
    purpose: str,
    algorithm_id: str = "none",
    replicate_index: int = 0,
) -> int:
    label = f"loa-seed-v1|{master_seed}|{problem_math_hash}|{purpose}|{algorithm_id}|{replicate_index}"
    return int.from_bytes(hashlib.sha256(label.encode("utf-8")).digest()[:8], "big") % 2**32


def validate_plan(
    plan: CandidatePlan, *, count: int, allowed_evidence_refs: Iterable[str] | None = None
) -> CandidatePlan:
    ids = [row.algorithm_id for row in plan.candidates]
    if len(ids) != count or len(set(ids)) != count:
        raise DomainError("PLAN_VALIDATION_ERROR", "تعداد گزینه‌های متمایز با سیاست مطابقت ندارد.")
    allowed = set(allowed_evidence_refs) if allowed_evidence_refs is not None else None
    for row in plan.candidates:
        if SOLVER_CONFIG_IDS.get(row.algorithm_id) != row.config_id:
            raise DomainError("PLAN_VALIDATION_ERROR", "روش یا پیکربندی در Registry ثبت نشده است.")
        if allowed is not None and not set(row.evidence_refs) <= allowed:
            raise DomainError("PLAN_VALIDATION_ERROR", "ارجاع شاهد خارج از context است.")
    return plan


def _configured_plan(algorithm_ids: list[str], rationale: str, notes: str | None = None) -> CandidatePlan:
    return CandidatePlan(
        candidates=[
            Candidate(
                algorithm_id=algorithm_id,
                config_id=SOLVER_CONFIG_IDS[algorithm_id],
                rationale_fa=rationale,
                evidence_refs=[f"solver:{algorithm_id}"],
            )
            for algorithm_id in algorithm_ids
        ],
        uncertainty_notes=notes,
    )


def propose_baseline(policy: ExecutionPolicy, revision: ProblemRevision) -> CandidatePlan:
    algorithm_ids = sorted(SOLVER_CONFIG_IDS)
    if policy.selection_policy == "fixed_solver":
        return _configured_plan([policy.fixed_algorithm_id], "روش ثابت در سیاست کاربر ثبت شده است.")
    if policy.selection_policy == "random_solver":
        seed = derive_seed(policy.master_seed, revision.problem_math_hash, "selection")
        algorithm = algorithm_ids[int(rng(seed).integers(len(algorithm_ids)))]
        return _configured_plan([algorithm], "روش با نمونه‌گیری یکنواخت و seed تثبیت‌شده انتخاب شد.")
    if policy.selection_policy == "rule_based":
        algorithm = "de.original" if len(revision.spec.variables) <= 10 else "cma_es.mealpy"
        return _configured_plan([algorithm], "قاعدهٔ پژوهشی بر اساس تعداد متغیرها اعمال شد.")
    if policy.selection_policy == "pilot_only":
        return _configured_plan(algorithm_ids, "همهٔ روش‌های ثبت‌شده وارد پایلوت می‌شوند.")
    raise DomainError("PLAN_VALIDATION_ERROR", "این سیاست به پیشنهاد مشاور یا fallback ثبت‌شده نیاز دارد.")


def fallback_plan(selection_policy: str) -> CandidatePlan:
    if selection_policy == "llm_only":
        ids = ["de.original"]
    elif selection_policy == "llm_shortlist_then_pilot":
        ids = ["abc.original", "de.original", "pso.original"]
    else:
        raise DomainError("PLAN_VALIDATION_ERROR", "fallback مشاور برای این سیاست تعریف نشده است.")
    return _configured_plan(
        ids,
        "پاسخ ساختاری مشاور نامعتبر بود و سیاست جایگزین ثبت‌شده اعمال شد.",
        "این پیشنهاد از سیاست ثابت آمده است.",
    )


def fixed_decision(plan: CandidatePlan, source: str) -> SelectionDecision:
    candidate = validate_plan(plan, count=1).candidates[0]
    return SelectionDecision(
        decision_id=str(uuid4()),
        selection_status="fixed_by_policy",
        decision_source=source,
        selected_algorithm_id=candidate.algorithm_id,
        selected_config_id=candidate.config_id,
        metrics=[],
        scale=None,
        tie_break_order=[],
        uncertainty_notes=plan.uncertainty_notes,
    )


def _valid_pilot(trial: TrialRequest, result: ExecutionResult) -> bool:
    return (
        trial.phase == result.phase == "pilot"
        and trial.attempt_id == result.execution_id
        and trial.job_id == result.job_id
        and trial.evaluation_quota == PILOT_EVALUATIONS
        and result.execution_status == "completed"
        and result.termination_reason == "evaluation_budget"
        and result.started_evaluations == result.successful_evaluations == PILOT_EVALUATIONS
        and result.failed_evaluations == result.rejected_proposals == 0
        and result.error is None
        and result.history_complete
        and result.history_sha256 is not None
        and result.best is not None
        and result.best.job_id == trial.job_id
        and result.best.trial_id == trial.trial_id
        and result.best.execution_id == result.execution_id
        and result.best.problem_math_hash == trial.problem_math_hash
        and result.best.status == "successful"
    )


def rank_pilots(pilots: list[tuple[TrialRequest, ExecutionResult]], plan: CandidatePlan) -> SelectionDecision:
    validate_plan(plan, count=len(plan.candidates))
    grouped = {row.algorithm_id: [] for row in plan.candidates}
    for trial, result in pilots:
        if trial.algorithm_id in grouped:
            grouped[trial.algorithm_id].append((trial, result))

    eligible_scores = {}
    for algorithm_id, rows in grouped.items():
        if (
            len(rows) == PILOT_REPEATS
            and {trial.replicate_index for trial, _ in rows} == set(range(PILOT_REPEATS))
            and all(_valid_pilot(trial, result) for trial, result in rows)
        ):
            eligible_scores[algorithm_id] = [result.best.score_for_minimization for _, result in rows]

    scale = max([1.0] + [abs(score) for scores in eligible_scores.values() for score in scores])
    metrics = []
    for algorithm_id, rows in sorted(grouped.items()):
        scores = eligible_scores.get(algorithm_id)
        if scores is None:
            median_score, iqr = None, None
            reason = "INCOMPLETE_PILOT_SET" if len(rows) != PILOT_REPEATS else "INELIGIBLE_PILOT_RESULT"
        else:
            median_score = stable_median(scores)
            low, high = np.quantile(np.array(scores, dtype=np.float64) / scale, [0.25, 0.75], method="linear")
            iqr = float(high - low)
            reason = None
        metrics.append(
            PilotMetric(
                algorithm_id=algorithm_id,
                eligible=scores is not None,
                trial_ids=[trial.trial_id for trial, _ in sorted(rows, key=lambda pair: pair[0].replicate_index)],
                median_score=median_score,
                normalized_iqr=iqr,
                reason=reason,
            )
        )

    eligible = sorted(
        (metric for metric in metrics if metric.eligible),
        key=lambda metric: (metric.median_score, metric.normalized_iqr, metric.algorithm_id),
    )
    winner = eligible[0].algorithm_id if len(eligible) >= 2 else None
    return SelectionDecision(
        decision_id=str(uuid4()),
        selection_status="selected" if winner else "insufficient_evidence",
        decision_source="empirical_pilot_selection",
        selected_algorithm_id=winner,
        selected_config_id=SOLVER_CONFIG_IDS[winner] if winner else None,
        metrics=metrics,
        scale=scale,
        tie_break_order=TIE_BREAK_ORDER.copy(),
        uncertainty_notes="رتبه‌بندی توصیفی بر پایهٔ سه تکرار است و معناداری آماری را اثبات نمی‌کند.",
    )
