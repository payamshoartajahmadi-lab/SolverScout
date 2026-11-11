"""Explicit bounded qualification of installed configurations in numeric subprocesses."""

from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from p_mc_optimizer.catalog.adapter import environment_id
from p_mc_optimizer.catalog.adapter import seed_identity

from p_mc_optimizer.catalog.models import QualificationCheck, QualificationReport, SolverConfiguration, digest
from p_mc_optimizer.catalog.trials import _request_for_configuration
from p_mc_optimizer.domain.models import DomainError, ExecutionRequest, ProblemInput, strict_json_loads
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
NUMERIC_POLICY_FE_PER_CONFIG = 31996
NUMERIC_POLICY_CASES = (
    ("sphere_2d", 2, "sphere", "pilot", 500),
    ("sphere_2d_replay", 2, "sphere", "pilot", 500),
    ("shifted_5d", 5, "q07", "main", 14998),
    ("shifted_5d_replay", 5, "q07", "main", 14998),
    ("shifted_30d", 30, "shifted", "pilot", 500),
    ("controlled_failure", 2, "fault", "pilot", 500),
)


def _sum(terms: list[str]) -> str:
    # Balance the DSL expression so a 30-dimensional test fits its AST depth limit.
    if len(terms) == 1:
        return terms[0]
    midpoint = len(terms) // 2
    return f"({_sum(terms[:midpoint])}+{_sum(terms[midpoint:])})"


def _problem(dimension: int, kind: str):
    if kind == "q07":
        return freeze_problem(ProblemInput.model_validate({
            "schema_version": "1.0",
            "variables": [{"name": f"x{i}", "label_fa": None, "type": "real", "lower": -5.0,
                           "upper": 5.0, "unit": None} for i in range(1, 6)],
            "objective": {"sense": "minimize", "unit": None, "source": {"kind": "expression", "expression":
                "(x1-1)**2+2*(x2+2)**2+3*(x3-0.5)**2+4*(x4-3)**2+5*(x5+1)**2"}},
            "parameters": {}, "constraints": [], "deterministic": True,
        }))
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


def numeric_check_matrix():
    return [{
        "check_id": name, "problem_specification": _problem(dimension, kind).spec.model_dump(mode="json", exclude={"source_map"}),
        "phase": phase, "evaluation_quota": quota, "replicate_index": 0,
        "seed_rule": {"master_seed": 1405, "phase": "budget_policy_qualification", "replicate_index": 0},
    } for name, dimension, kind, phase, quota in NUMERIC_POLICY_CASES]


def numeric_check_request(config, entry, snapshot, qualification_id, *, remaining):
    from p_mc_optimizer.catalog.trials import apply_budget_configuration

    revision = freeze_problem(ProblemInput.model_validate(entry["problem_specification"]))
    job_id, trial_id, attempt_id = (str(uuid4()) for _ in range(3))
    seed = derive_seed(1405, revision.problem_math_hash, "budget_policy_qualification",
                       seed_identity(config.implementation_id), 0)
    trial = _request_for_configuration(
        revision, configuration=config, catalog_hash=digest([]), job_id=job_id, trial_id=trial_id,
        attempt_id=attempt_id, idempotency_key=attempt_id, seed=seed, phase=entry["phase"],
        replicate_index=entry["replicate_index"], evaluation_quota=entry["evaluation_quota"],
        deadline_seconds=min(60.0, remaining),
    )
    authorization = {"kind": "qualification_run", "qualification_id": qualification_id,
                     "check_id": entry["check_id"], "certificate_hash": None}
    trial = apply_budget_configuration(trial, snapshot, authorization)
    return ExecutionRequest(
        job_id=job_id, execution_id=attempt_id, phase=entry["phase"], revision=revision, seed=seed,
        evaluation_quota=trial.evaluation_quota, deadline_seconds=trial.deadline_seconds, trial=trial,
    )


def _numeric_history(result, request, path):
    import hashlib
    import math

    if not result.history_complete or not path.is_file() or result.rejected_proposals:
        return False, None
    content = path.read_bytes()
    rows = [strict_json_loads(line) for line in content.splitlines()]
    if (hashlib.sha256(content).hexdigest() != result.history_sha256 or len(rows) != result.started_evaluations
            or not rows or rows[0]["evaluation_kind"] != "library_setup"
            or any(not all(math.isfinite(x) and var.lower <= x <= var.upper
                           for x, var in zip(row["x"], request.revision.spec.variables, strict=True)) for row in rows)):
        return False, None
    if request.trial.numeric_execution.authorization.kind == "qualification_run" and (
        request.trial.numeric_execution.authorization.check_id == "controlled_failure"
    ):
        return (result.execution_status == "failed" and result.error is not None
                and result.error.error_code == "OBJECTIVE_EVALUATION_FAILED"
                and result.started_evaluations == result.failed_evaluations == 1
                and result.successful_evaluations == 0), None
    derivation = request.trial.numeric_execution.derivation
    expected = derivation.expected_started_without_error
    valid = (result.execution_status == "completed" and result.started_evaluations == result.successful_evaluations == expected
             and result.failed_evaluations == 0 and result.error is None and result.best is not None
             and result.termination_reason == ("evaluation_budget" if expected == request.evaluation_quota else "solver_stop")
             and all(math.isfinite(row["objective_raw"]) for row in rows)
             and result.best.objective_raw == min(row["objective_raw"] for row in rows)
             and result.best.model_dump(mode="json") in rows)
    trace = digest([(row["x"], row["objective_raw"], row["evaluation_kind"]) for row in rows]) if valid else None
    return valid, trace


def qualify_numeric_policy(config, *, snapshot, qualification_id, history_directory, deadline, cancelled, reserve, finish):
    from p_mc_optimizer.catalog.budget_policy import numeric_runtime_fingerprint_hash

    checks, traces = [], {}
    for entry in snapshot["check_matrix"]:
        if cancelled() or monotonic() >= deadline:
            break
        if numeric_runtime_fingerprint_hash() != snapshot["numeric_runtime_fingerprint_sha256"]:
            raise DomainError("NUMERIC_POLICY_ENVIRONMENT_MISMATCH", "محیط تأیید تنظیم با snapshot یکسان نیست.")
        request = numeric_check_request(config, entry, snapshot, qualification_id, remaining=deadline-monotonic())
        authority = reserve(entry, request)
        path = history_directory / (entry["check_id"] + ".jsonl")
        result = run_execution(request, history_path=path, cancelled=cancelled, _qualification_authority=authority)
        passed, trace = _numeric_history(result, request, path)
        name = entry["check_id"]
        outcome = result.error.error_code if result.error else result.termination_reason
        if name.endswith("_replay"):
            if trace is None or trace != traces.get(name.removesuffix("_replay")):
                passed, outcome = False, "SEED_REPLAY_MISMATCH"
        else:
            traces[name] = trace
        status = "cancelled" if result.execution_status == "cancelled" else ("passed" if passed else "failed")
        check = QualificationCheck(
            check=name, status=status, started_evaluations=result.started_evaluations,
            charged_unknown=0 if result.history_complete else request.evaluation_quota-result.started_evaluations,
            history_sha256=result.history_sha256, outcome=outcome, elapsed_seconds=result.elapsed_compute_seconds,
        )
        finish(check)
        checks.append(check.model_dump(mode="json"))
        if not passed:
            break
    pilot_compatible = len(checks) == len(snapshot["check_matrix"]) and all(row["status"] == "passed" for row in checks)
    pilot_compatible = pilot_compatible and all(row["started_evaluations"] == 500 for row in checks
        if row["check"] in {"sphere_2d", "sphere_2d_replay", "shifted_30d"})
    was_cancelled = cancelled()
    pilot_compatible = pilot_compatible and not was_cancelled
    return {
        "schema_version": "1.0", "qualification_id": qualification_id, "base_config_id": config.config_id,
        "implementation_id": config.implementation_id, "policy_id": snapshot["policy_id"],
        "policy_hash": snapshot["policy_hash"], "numeric_runtime_fingerprint": snapshot["numeric_runtime_fingerprint"],
        "numeric_runtime_fingerprint_sha256": snapshot["numeric_runtime_fingerprint_sha256"],
        "status": "qualified" if pilot_compatible else ("cancelled" if was_cancelled else "rejected"),
        "pilot_500_compatible": pilot_compatible,
        "quota_domain": {"exclusive_minimum": 1+config.kwargs()["pop_size"], "rule": "Q>1+P"},
        "checks": checks,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }


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
