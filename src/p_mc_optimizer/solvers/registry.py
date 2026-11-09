"""Fixed SolverCard registry and thin MEALPY adapters."""

from __future__ import annotations

from importlib.metadata import version

from p_mc_optimizer.domain.models import (
    EVALUATOR_VERSION,
    SOLVER_CONFIG_IDS,
    DomainError,
    ProblemRevision,
    TrialRequest,
)
from p_mc_optimizer.execution.evaluator import BudgetedEvaluator


REGISTRY_VERSION = "1.0"
ADAPTER_VERSION = "1.0"
MEALPY_VERSION = "3.0.3"
_DEFAULTS = {"epoch": 100_000, "pop_size": 30}
_PARAMETERS = {
    "abc.original": _DEFAULTS | {"n_limits": 50},
    "cma_es.mealpy": _DEFAULTS.copy(),
    "de.original": _DEFAULTS | {"wf": 0.7, "cr": 0.9, "strategy": 0},
    "pso.original": _DEFAULTS | {"c1": 2.05, "c2": 2.05, "w": 0.4},
}
_CLASSES = {
    "abc.original": "mealpy.swarm_based.ABC.OriginalABC",
    "cma_es.mealpy": "mealpy.evolutionary_based.ES.CMA_ES",
    "de.original": "mealpy.evolutionary_based.DE.OriginalDE",
    "pso.original": "mealpy.swarm_based.PSO.OriginalPSO",
}


def solver_cards() -> list[dict]:
    return [
        {
            "algorithm_id": solver_id,
            "config_id": SOLVER_CONFIG_IDS[solver_id],
            "class_path": _CLASSES[solver_id],
            "library_version": MEALPY_VERSION,
            "adapter_version": ADAPTER_VERSION,
            "configuration_snapshot": _PARAMETERS[solver_id].copy(),
            "boundary_repair": "MEALPY uniform resampling then FloatVar clipping" if solver_id == "pso.original" else "MEALPY FloatVar clipping",
            "rng": "MEALPY default_rng (PCG64) and local random.Random, seeded",
            "mode": "single",
            "minmax": "min",
        }
        for solver_id in sorted(SOLVER_CONFIG_IDS)
    ]


def trial_request(
    revision: ProblemRevision,
    *,
    job_id: str,
    trial_id: str,
    attempt_id: str,
    idempotency_key: str,
    algorithm_id: str,
    seed: int,
    phase: str,
    replicate_index: int,
    evaluation_quota: int,
    deadline_seconds: float,
) -> TrialRequest:
    return TrialRequest(
        job_id=job_id,
        trial_id=trial_id,
        attempt_id=attempt_id,
        idempotency_key=idempotency_key,
        problem_revision_id=revision.problem_revision_id,
        problem_math_hash=revision.problem_math_hash,
        evaluation_contract_hash=revision.evaluation_contract_hash,
        evaluator_version=EVALUATOR_VERSION,
        algorithm_id=algorithm_id,
        config_id=SOLVER_CONFIG_IDS[algorithm_id],
        adapter_version=ADAPTER_VERSION,
        library_version=MEALPY_VERSION,
        configuration_snapshot=_PARAMETERS[algorithm_id].copy(),
        seed=seed,
        phase=phase,
        replicate_index=replicate_index,
        evaluation_quota=evaluation_quota,
        deadline_seconds=deadline_seconds,
    )


def run_solver(evaluator: BudgetedEvaluator) -> None:
    trial = evaluator.request.trial
    if (
        version("mealpy") != MEALPY_VERSION
        or trial.library_version != MEALPY_VERSION
        or trial.adapter_version != ADAPTER_VERSION
        or trial.configuration_snapshot != _PARAMETERS[trial.algorithm_id]
    ):
        raise DomainError("SOLVER_CONFIGURATION_MISMATCH", "نسخه یا پیکربندی Solver با Registry تثبیت‌شده تطابق ندارد.")

    from mealpy import FloatVar, Problem
    from mealpy.evolutionary_based import DE, ES
    from mealpy.swarm_based import ABC, PSO

    classes = {
        "abc.original": ABC.OriginalABC,
        "cma_es.mealpy": ES.CMA_ES,
        "de.original": DE.OriginalDE,
        "pso.original": PSO.OriginalPSO,
    }
    model = classes[trial.algorithm_id](**trial.configuration_snapshot)
    problem = Problem(
        bounds=FloatVar(lb=tuple(evaluator.lower), ub=tuple(evaluator.upper), name="x"),
        minmax="min",
        obj_func=evaluator,
        log_to=None,
        save_population=False,
    )
    # MEALPY's lazy objective-cardinality check invokes obj_func. Probe it with
    # the trial's seed and account for the call using the same evaluator.
    problem.set_seed(trial.seed)
    evaluator.evaluation_kind = "library_setup"
    try:
        if problem.n_objs != 1:
            raise DomainError("SOLVER_CONFIGURATION_MISMATCH", "Adapter به تابع تک‌هدفه نیاز دارد.")
    finally:
        evaluator.evaluation_kind = "objective"

    model.solve(problem=problem, mode="single", seed=trial.seed)
