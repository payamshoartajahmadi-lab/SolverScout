"""Deterministic sampling, descriptive profiling, and independent verification."""

from __future__ import annotations

import math

import numpy as np

from p_mc_optimizer.domain.models import (
    EVALUATOR_VERSION,
    NUMERIC_ATOL,
    NUMERIC_RTOL,
    PREFLIGHT_EVALUATIONS,
    PROFILE_EVALUATIONS,
    DomainError,
    EvaluationRecord,
    ExecutionRequest,
    ProblemProfile,
    ValidationResult,
)
from p_mc_optimizer.execution.evaluator import BudgetedEvaluator


def rng(seed: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(seed))


def uniform_interior(seed: int, lower: np.ndarray, upper: np.ndarray, count: int) -> np.ndarray:
    normalized = rng(seed).random((count, len(lower)))
    points = lower + (upper - lower) * normalized
    # At the edges, the conversion from normalized coordinates can round to a bound.
    return np.clip(points, np.nextafter(lower, upper), np.nextafter(upper, lower))


def numerically_equal(a: float, b: float) -> bool:
    return abs(a - b) <= NUMERIC_ATOL + NUMERIC_RTOL * max(abs(a), abs(b))


def run_preflight(evaluator: BudgetedEvaluator) -> None:
    points = uniform_interior(evaluator.request.seed, evaluator.lower, evaluator.upper, PREFLIGHT_EVALUATIONS // 2)
    for point in points:
        left = evaluator(point)
        right = evaluator(point)
        if not numerically_equal(left, right):
            raise DomainError("DETERMINISM_CHECK_FAILED", "تابع هدف در تکرار همان نقطه مقدار سازگار تولید نکرد.")


def run_profile(evaluator: BudgetedEvaluator) -> None:
    for point in uniform_interior(evaluator.request.seed, evaluator.lower, evaluator.upper, PROFILE_EVALUATIONS):
        evaluator(point)


def stable_median(values: list[float]) -> float:
    ordered = sorted(values)
    half = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[half]
    lower, upper = ordered[half - 1 : half + 1]
    distance = upper - lower
    return lower + distance / 2 if math.isfinite(distance) else lower / 2 + upper / 2


def profile_features(request: ExecutionRequest, records: list[EvaluationRecord]) -> ProblemProfile:
    lower = [variable.lower for variable in request.revision.spec.variables]
    upper = [variable.upper for variable in request.revision.spec.variables]
    values = [record.score_for_minimization for record in records]
    scale = max(1.0, max(map(abs, values)))
    normalized = np.array(values, dtype=np.float64) / scale
    low_quartile, high_quartile = np.quantile(normalized, [0.25, 0.75], method="linear")
    return ProblemProfile(
        n_variables=len(lower),
        lower=lower,
        upper=upper,
        widths=[hi - lo for lo, hi in zip(lower, upper, strict=True)],
        objective_sense=request.revision.spec.objective.sense,
        sample_count=len(records),
        objective_scale=scale,
        score_min=min(values),
        score_median=stable_median(values),
        score_max=max(values),
        normalized_iqr=float(high_quartile - low_quartile),
        normalized_std=float(np.std(normalized)),
        mean_evaluation_seconds=math.fsum(record.evaluation_seconds for record in records) / len(records),
    )


def run_verification(evaluator: BudgetedEvaluator) -> None:
    request = evaluator.request
    reference = request.verification_reference
    if (
        reference.job_id != request.job_id
        or reference.problem_math_hash != request.revision.problem_math_hash
        or reference.evaluator_version != EVALUATOR_VERSION
        or reference.phase == "verification"
        or reference.status != "successful"
    ):
        raise DomainError("VERIFICATION_REFERENCE_MISMATCH", "ارزیابی مرجع با snapshot این اجرا سازگار نیست.")
    evaluator(reference.x)
    evaluator(reference.x)


def validation_result(request: ExecutionRequest, records: list[EvaluationRecord], execution_status: str) -> ValidationResult:
    reference = request.verification_reference
    original = reference.objective_raw
    values = [record.objective_raw for record in records if record.status == "successful"]
    pairs = [(original, value) for value in values]
    if len(values) == 2:
        pairs.append((values[0], values[1]))

    if execution_status in {"timed_out", "cancelled", "unknown_outcome"}:
        status = "incomplete"
    elif execution_status != "completed" or len(values) != 2:
        status = "failed"
    else:
        status = "passed" if all(numerically_equal(a, b) for a, b in pairs) else "failed"

    differences = [abs(a - b) for a, b in pairs]
    return ValidationResult(
        validation_status=status,
        reference_evaluation_id=reference.evaluation_id,
        reference_objective_raw=original,
        repeat_objective_raw=values,
        absolute_differences=[difference if math.isfinite(difference) else None for difference in differences],
    )
