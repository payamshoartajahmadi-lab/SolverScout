"""Explicit bounded qualification of installed configurations in numeric subprocesses."""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from uuid import uuid4

from p_mc_optimizer.catalog.adapter import environment_id

from p_mc_optimizer.catalog.models import QualificationCheck, QualificationReport, SolverConfiguration, digest
from p_mc_optimizer.catalog.trials import _request_for_configuration
from p_mc_optimizer.domain.models import ExecutionRequest, ProblemInput, strict_json_loads
from p_mc_optimizer.execution.supervisor import run_execution
from p_mc_optimizer.problems.registry import freeze_problem
from p_mc_optimizer.selection.policies import derive_seed

QUALIFICATION_FE_PER_CHECK = 500
QUALIFICATION_CASES = (
    ("sphere_2d", 2, "sphere"),
    ("sphere_2d_replay", 2, "sphere"),
    ("rosenbrock_2d", 2, "rosenbrock"),
    ("shifted_10d", 10, "shifted"),
    ("shifted_30d", 30, "shifted"),
    ("controlled_failure", 2, "fault"),
)
QUALIFICATION_FE_PER_CONFIG = QUALIFICATION_FE_PER_CHECK * len(QUALIFICATION_CASES)


def _sum(terms: list[str]) -> str:
    # Balance the DSL expression so a 30-dimensional test fits its AST depth limit.
    if len(terms) == 1:
        return terms[0]
    midpoint = len(terms) // 2
    return f"({_sum(terms[:midpoint])}+{_sum(terms[midpoint:])})"


def _problem(dimension: int, kind: str):
    names = [f"x{i}" for i in range(dimension)]
    expression = {
        "sphere": _sum([f"{name}**2" for name in names]),
        "shifted": _sum([f"({name}-0.5)**2" for name in names]),
        "rosenbrock": "100*(x1-x0**2)**2+(1-x0)**2",
        "fault": "1/(x0-x0)",
    }[kind]
    return freeze_problem(ProblemInput.model_validate({
        "schema_version": "1.0",
        "variables": [{"name": name, "label_fa": None, "type": "real", "lower": -5.0, "upper": 5.0, "unit": None}
                      for name in names],
        "objective": {"sense": "minimize", "unit": None, "source": {"kind": "expression", "expression": expression}},
        "parameters": {}, "constraints": [], "deterministic": True,
    }))


def _qualified_history(result, path: Path, *, expected_fault: bool):
    if not result.history_complete or result.rejected_proposals:
        return False, None
    rows = [strict_json_loads(line) for line in path.read_bytes().splitlines()]
    if (
        len(rows) != result.started_evaluations
        or not rows
        or rows[0]["evaluation_kind"] != "library_setup"
        or any(not all(-5.0 <= x <= 5.0 for x in row["x"]) for row in rows)
    ):
        return False, None
    if expected_fault:
        valid = (
            result.execution_status == "failed" and result.error is not None
            and result.error.error_code == "OBJECTIVE_EVALUATION_FAILED"
            and result.started_evaluations == result.failed_evaluations == 1
            and result.successful_evaluations == 0
        )
        return valid, None
    valid = (
        result.execution_status == "completed" and result.termination_reason == "evaluation_budget"
        and result.started_evaluations == result.successful_evaluations == QUALIFICATION_FE_PER_CHECK
        and result.failed_evaluations == 0 and result.best is not None
        and result.best.objective_raw == min(row["objective_raw"] for row in rows)
    )
    trace = digest([(row["x"], row["objective_raw"], row["evaluation_kind"]) for row in rows]) if valid else None
    return valid, trace


def qualify_configuration(
    config: SolverConfiguration,
    *,
    history_directory: Path,
    deadline: float,
    cancelled,
    reserve,
    finish,
) -> QualificationReport:
    checks = []
    replay_trace = None
    for name, dimension, kind in QUALIFICATION_CASES:
        remaining = deadline - monotonic()
        if cancelled() or remaining <= 0:
            break
        revision = _problem(dimension, kind)
        job_id, trial_id, attempt_id = (str(uuid4()) for _ in range(3))
        seed = derive_seed(1405, revision.problem_math_hash, "qualification", config.implementation_id)
        trial = _request_for_configuration(
            revision, configuration=config, catalog_hash=digest([]), job_id=job_id, trial_id=trial_id,
            attempt_id=attempt_id, idempotency_key=attempt_id, seed=seed, phase="pilot",
            replicate_index=0, evaluation_quota=QUALIFICATION_FE_PER_CHECK,
            deadline_seconds=min(10.0, remaining),
        )
        request = ExecutionRequest(
            job_id=job_id, execution_id=attempt_id, phase="pilot", revision=revision, seed=seed,
            evaluation_quota=trial.evaluation_quota, deadline_seconds=trial.deadline_seconds, trial=trial,
        )
        path = history_directory / (name + ".jsonl")
        reserve(name, request.evaluation_quota)
        result = run_execution(request, history_path=path, cancelled=cancelled)
        passed, trace = _qualified_history(result, path, expected_fault=kind == "fault")
        outcome = result.error.error_code if result.error else result.termination_reason
        if name == "sphere_2d":
            replay_trace = trace
        if name == "sphere_2d_replay" and trace != replay_trace:
            passed, outcome = False, "SEED_REPLAY_MISMATCH"
        status = "passed" if passed else "failed"
        if result.execution_status == "cancelled":
            status = "cancelled"
        check = QualificationCheck(
            check=name, status=status, started_evaluations=result.started_evaluations,
            charged_unknown=0 if result.history_complete else request.evaluation_quota - result.started_evaluations,
            history_sha256=result.history_sha256, outcome=outcome, elapsed_seconds=result.elapsed_compute_seconds,
        )
        finish(check)
        checks.append(check)
        if status != "passed":
            break

    qualified = len(checks) == len(QUALIFICATION_CASES) and all(check.status == "passed" for check in checks)
    status = "qualified" if qualified else ("cancelled" if cancelled() else "rejected")
    return QualificationReport(
        config_id=config.config_id, status=status, checks=tuple(checks),
        pilot_500_compatible=qualified, environment=environment_id(),
    )
