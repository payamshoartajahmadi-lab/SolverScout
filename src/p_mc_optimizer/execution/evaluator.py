"""The sole objective-call boundary for all numerical phases."""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from uuid import uuid4

import numpy as np

from p_mc_optimizer.domain.models import EVALUATOR_VERSION, DomainError, EvaluationRecord, ExecutionRequest


class EvaluationBudgetExhausted(Exception):
    pass


class ExecutionCancelled(Exception):
    pass


class ExecutionTimedOut(Exception):
    pass


class BudgetedEvaluator:
    def __init__(
        self,
        request: ExecutionRequest,
        objective: Callable,
        *,
        deadline: float,
        should_cancel: Callable[[], bool],
        start: Callable[[int], None],
        record: Callable[[EvaluationRecord], None],
    ):
        self.request = request
        self.objective = objective
        self.deadline = deadline
        self.should_cancel = should_cancel
        self.on_start = start
        self.on_record = record
        self.lower = np.array([v.lower for v in request.revision.spec.variables], dtype=np.float64)
        self.upper = np.array([v.upper for v in request.revision.spec.variables], dtype=np.float64)
        self.maximize = request.revision.spec.objective.sense == "maximize"
        self.started = 0
        self.successful = 0
        self.failed = 0
        self.rejected = 0
        self.best: EvaluationRecord | None = None
        self.evaluation_kind = "objective"

    def validate_proposal(self, proposal) -> np.ndarray:
        try:
            # Use an object view to keep a mixed list containing bool from silently
            # becoming a valid float vector before its element types are checked.
            if isinstance(proposal, np.ndarray):
                values = proposal
                real_numeric = values.dtype.kind in {"f", "i", "u"}
            else:
                values = np.asarray(proposal, dtype=object)
                real_numeric = all(
                    isinstance(value, (float, int, np.floating, np.integer))
                    and not isinstance(value, (bool, np.bool_))
                    for value in values.flat
                )
            if not real_numeric or values.shape != self.lower.shape:
                raise ValueError
            point = np.array(values, dtype=np.float64, copy=True)
            if not np.isfinite(point).all() or np.any(point < self.lower) or np.any(point > self.upper):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            self.rejected += 1
            raise DomainError("INVALID_SOLVER_PROPOSAL", "نقطهٔ پیشنهادی از نظر بعد، نوع یا کران معتبر نیست.") from None
        return point

    def __call__(self, proposal) -> float:
        if self.should_cancel():
            raise ExecutionCancelled
        if monotonic() >= self.deadline:
            raise ExecutionTimedOut
        point = self.validate_proposal(proposal)
        if self.started >= self.request.evaluation_quota:
            raise EvaluationBudgetExhausted

        self.started += 1
        try:
            self.on_start(self.started)
        except (ExecutionCancelled, ExecutionTimedOut):
            self.started -= 1
            raise
        started_at = monotonic()
        snapshot = point.tolist()
        try:
            value = self.objective(point)
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
                raise ValueError("Objective result is not a real scalar")
            raw = float(value)
            if not np.isfinite(raw):
                raise ArithmeticError("Objective result is not finite")
        except Exception as error:
            self.failed += 1
            self.on_record(self._record(snapshot, started_at, None))
            raise DomainError("OBJECTIVE_EVALUATION_FAILED", "تابع هدف در نقطهٔ ثبت‌شده مقدار حقیقی متناهی تولید نکرد.") from error

        self.successful += 1
        observation = self._record(snapshot, started_at, raw)
        if self.request.phase != "verification" and (
            self.best is None or observation.score_for_minimization < self.best.score_for_minimization
        ):
            self.best = observation
        self.on_record(observation)
        return observation.score_for_minimization

    def _record(self, point: list[float], started_at: float, raw: float | None) -> EvaluationRecord:
        request = self.request
        trial = request.trial
        score = (-raw if self.maximize else raw) if raw is not None else None
        return EvaluationRecord(
            job_id=request.job_id,
            execution_id=request.execution_id,
            trial_id=trial.trial_id if trial else None,
            attempt_id=trial.attempt_id if trial else None,
            stage_execution_id=None if trial else request.execution_id,
            evaluation_id=str(uuid4()),
            sequence=self.started,
            phase=request.phase,
            evaluation_kind=self.evaluation_kind,
            x=point,
            objective_raw=raw,
            score_for_minimization=score,
            status="successful" if raw is not None else "failed",
            evaluation_seconds=monotonic() - started_at,
            problem_math_hash=request.revision.problem_math_hash,
            evaluator_version=EVALUATOR_VERSION,
            error_code=None if raw is not None else "OBJECTIVE_EVALUATION_FAILED",
        )
