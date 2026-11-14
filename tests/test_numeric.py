"""Numerical and domain acceptance tests. Fault and spy objectives are TEST FIXTURES."""

from __future__ import annotations

import hashlib
import math
import multiprocessing as mp
import os
import time
from functools import partial
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from pydantic import ValidationError

from p_mc_optimizer.domain.models import (
    SOLVER_CONFIG_IDS,
    Candidate,
    CandidatePlan,
    DomainError,
    ExecutionPolicy,
    ExecutionPolicyInput,
    ExecutionRequest,
    ProblemInput,
    strict_json_loads,
)
from p_mc_optimizer.execution.evaluator import BudgetedEvaluator, EvaluationBudgetExhausted
from p_mc_optimizer.execution.supervisor import run_execution
from p_mc_optimizer.problems.dsl import parse_expression
from p_mc_optimizer.problems.registry import freeze_problem, objective_for, problem_hashes
from p_mc_optimizer.selection.policies import (
    SelectionContext, SelectionPolicy, derive_seed, propose_baseline, rank_pilots, validate_plan,
)
from p_mc_optimizer.solvers.registry import trial_request


def uid():
    return str(uuid4())


def problem_data(*, expression="(x1-2)**2+(x2+1)**2", sense="minimize"):
    return {
        "schema_version": "1.0",
        "variables": [
            {"name": "x1", "label_fa": None, "type": "real", "lower": -5, "upper": 5, "unit": None},
            {"name": "x2", "label_fa": None, "type": "real", "lower": -5, "upper": 5, "unit": None},
        ],
        "objective": {"sense": sense, "unit": None, "source": {"kind": "expression", "expression": expression}},
        "parameters": {},
        "constraints": [],
        "deterministic": True,
    }


def revision(**kwargs):
    return freeze_problem(ProblemInput.model_validate(problem_data(**kwargs)))


def request(phase, *, math_revision=None, seconds=10.0, quota=None, algorithm="de.original", job_id=None, reference=None):
    math_revision = math_revision or revision()
    trial = None
    execution_id, job_id = uid(), job_id or uid()
    seed = derive_seed(1405, math_revision.problem_math_hash, phase, algorithm if phase in {"pilot", "main"} else "none")
    if phase in {"pilot", "main"}:
        trial = trial_request(
            math_revision, job_id=job_id, trial_id=uid(), attempt_id=execution_id, idempotency_key=uid(),
            algorithm_id=algorithm, seed=seed, phase=phase, replicate_index=0, evaluation_quota=quota,
            deadline_seconds=seconds,
        )
    else:
        quota = quota or {"preflight": 8, "profile": 492, "verification": 2}[phase]
    return ExecutionRequest(
        job_id=job_id, execution_id=execution_id, phase=phase, revision=math_revision, seed=seed,
        evaluation_quota=quota, deadline_seconds=seconds, trial=trial, verification_reference=reference,
    )


def run(item, tmp_path, **kwargs):
    output = tmp_path / f"{item.execution_id}.jsonl"
    result = run_execution(item, history_path=output, **kwargs)
    if result.history_complete:
        contents = output.read_bytes()
        assert hashlib.sha256(contents).hexdigest() == result.history_sha256
        rows = [strict_json_loads(line) for line in contents.splitlines()]
        assert len(rows) == result.started_evaluations
    else:
        assert not output.exists()
        rows = [strict_json_loads(line) for line in output.with_suffix(".jsonl.part").read_bytes().splitlines()]
    return result, rows


@pytest.mark.parametrize("value", ["-5", True, float("nan"), float("inf")])
def test_dat_001_002_reject_invalid_real(value):
    data = problem_data()
    data["variables"][0]["lower"] = value
    with pytest.raises(ValidationError) as exc:
        ProblemInput.model_validate(data)
    assert exc.value.errors()[0]["loc"] == ("variables", 0, "lower")


@pytest.mark.parametrize("edit", [
    lambda p: p.update(constraints=[{"expression": "x1>0"}]),
    lambda p: p.update(variables=p["variables"][:1]),
    lambda p: p.update(variables=p["variables"] + [{"name": f"y{i}", "type": "real", "lower": 0, "upper": 1} for i in range(29)]),
    lambda p: p["variables"][1].update(type="integer"),
    lambda p: p["variables"][1].update(name="x1"),
    lambda p: p["variables"][0].update(lower=5),
    lambda p: p["variables"][0].update(lower=6),
    lambda p: p.update(objective=[p["objective"]]),
])
def test_dat_002_003_unsupported_domain(edit):
    data = problem_data()
    edit(data)
    with pytest.raises(ValidationError) as exc:
        ProblemInput.model_validate(data)
    assert exc.value.errors()[0]["type"] == "UNSUPPORTED_PROBLEM"


def test_dat_001_sys_010_strict_json_and_bounds():
    for text in ('{"a": 1, "a": 2}', '{"a": NaN}', '{"a": 1e500}', '{"a": 1} {}'):
        with pytest.raises(ValueError):
            strict_json_loads(text)
    for lower, upper in [(-1e308, 1e308), (1.0, math.nextafter(1.0, 2.0))]:
        data = problem_data()
        data["variables"][0].update(lower=lower, upper=upper)
        with pytest.raises(ValidationError) as exc:
            ProblemInput.model_validate(data)
        assert exc.value.errors()[0]["type"] == "BOUNDS_RANGE_UNSUPPORTED"
    with pytest.raises(ValidationError) as exc:
        ProblemInput.model_validate(problem_data() | {"source_map": []})
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("expression", [
    "x1.real", "__import__('os')", "(lambda: 1)()", "[x1 for x1 in (1,2)]", "x1[0]", "y+1",
    "x1^2", "2x1", "2(x1+1)", "x1**x2", "x1**(1+2)", "x1**17", "True", "sin(a=x1)",
    "sin(*x1)", "min(x1)", "ｘ1", "x1 # comment",
    " " * 4097 + "x1", "-(" * 40 + "x1" + ")" * 40, "max(" + ",".join(["max(" + ",".join(["x1"] * 32) + ")"] * 32) + ")",
])
def test_dat_004_005_006_expression_rejection(expression):
    with pytest.raises(DomainError) as exc:
        parse_expression(expression, {"x1", "x2"})
    assert exc.value.code in {"INVALID_EXPRESSION", "UNKNOWN_VARIABLE"}


def test_dat_007_expression_math_and_benchmarks():
    expression = parse_expression("sin(pi/2)+cos(0)+tan(0)+exp(0)+log(e)+sqrt(4)+abs(-2)+min(1,2)+max(1,2)+x1**-2", {"x1"})
    assert expression.evaluate({"x1": 2.0}) == pytest.approx(11.25, abs=1e-12)
    for name, point in {"sphere": [0, 0], "ackley": [0, 0], "rastrigin": [0, 0], "rosenbrock": [1, 1]}.items():
        data = problem_data()
        data["objective"]["source"] = {"kind": "registered_function", "function_id": name, "function_version": "1.0"}
        assert objective_for(freeze_problem(ProblemInput.model_validate(data)).spec)(point) == pytest.approx(0.0, abs=1e-12)


def test_dat_010_011_012_hashes_detach_and_map():
    data = problem_data(expression=" (x1 - 2)**2  +  (x2 + 1)**2 ")
    p = ProblemInput.model_validate(data)
    first = freeze_problem(p)
    assert first.problem_math_hash == revision().problem_math_hash
    data["variables"][0]["label_fa"] = "آزمایش"
    display_revision = freeze_problem(ProblemInput.model_validate(data))
    assert first.problem_math_hash == display_revision.problem_math_hash
    data["variables"][0]["upper"] = 6
    next_revision = freeze_problem(ProblemInput.model_validate(data), problem_id=first.problem_id, revision_no=2)
    assert first.spec.variables[0].upper == 5
    assert next_revision.problem_id == first.problem_id
    assert next_revision.problem_math_hash != first.problem_math_hash
    data["variables"].reverse()
    reordered = freeze_problem(ProblemInput.model_validate(data))
    assert reordered.problem_math_hash != next_revision.problem_math_hash
    assert objective_for(reordered.spec)([-1, 2]) == 0
    assert any(row.problem_field_path == "/objective/source/expression" and row.source_sha256 for row in first.spec.source_map)


def test_exe_007_008_invalid_proposals_and_archive_copy():
    event_starts, observations = [], []
    item = request("main", quota=2)
    evaluator = BudgetedEvaluator(
        item, objective_for(item.revision.spec), deadline=time.monotonic() + 1, should_cancel=lambda: False,
        start=event_starts.append, record=observations.append,
    )
    for point in ([0], [0, 7], [True, 0], [math.nan, 0], np.array([1, 2], dtype=np.complex128)):
        with pytest.raises(DomainError, match="بعد، نوع یا کران"):
            evaluator(point)
    assert evaluator.rejected == 5
    assert event_starts == observations == []
    point = np.array([2.0, -1.0])
    evaluator(point)
    point[0] = 5
    assert evaluator.best.x == [2.0, -1.0]
    assert evaluator.best.objective_raw == 0.0
    evaluator(point)
    with pytest.raises(EvaluationBudgetExhausted):
        evaluator(point)
    assert evaluator.started == len(event_starts) == len(observations) == 2


def spy_objective(x, *, counter):
    counter.value += 1
    return (float(x[0]) - 2) ** 2 + (float(x[1]) + 1) ** 2


def fails_second(x, *, counter):
    counter.value += 1
    if counter.value == 2:
        raise RuntimeError("TEST FIXTURE objective fault")
    return 1.0


def hang(x, *, process_id):
    process_id.value = os.getpid()
    time.sleep(30)
    return 0.0


def dies(x):
    os._exit(75)


def environment_probe(x):
    assert not any("PMCO_TEST" in key for key in os.environ), "TEST FIXTURE environment leak"
    return 1.0


def stateful(x, *, counter):
    counter.value += 1
    return float(counter.value)


def nonfinite(x, *, value):
    return value


@pytest.mark.parametrize("algorithm", sorted(SOLVER_CONFIG_IDS))
def test_exe_001_002_003_real_mealpy_and_central_quota(tmp_path, algorithm):
    counter = mp.get_context("spawn").Value("i", 0)
    objective = partial(spy_objective, counter=counter)
    item = request("main", quota=7, algorithm=algorithm)
    result, rows = run(item, tmp_path, _objective_override=objective)
    assert result.execution_status == "completed"
    assert result.termination_reason == "evaluation_budget"
    assert counter.value == result.started_evaluations == result.successful_evaluations == 7
    assert rows[0]["evaluation_kind"] == "library_setup"
    assert [row["sequence"] for row in rows] == list(range(1, 8))
    assert result.best.evaluation_id == min(rows, key=lambda row: row["score_for_minimization"])["evaluation_id"]


@pytest.mark.parametrize("algorithm", sorted(SOLVER_CONFIG_IDS))
def test_real_mealpy_pilot_quality_and_mid_generation_archive(tmp_path, algorithm):
    item = request("pilot", quota=500, algorithm=algorithm)
    result, rows = run(item, tmp_path)
    assert result.execution_status == "completed"
    assert result.termination_reason == "evaluation_budget"
    assert result.started_evaluations == result.successful_evaluations == 500
    assert result.best.evaluation_id == min(rows, key=lambda row: row["objective_raw"])["evaluation_id"]
    x1, x2 = result.best.x
    assert result.best.objective_raw == pytest.approx((x1 - 2) ** 2 + (x2 + 1) ** 2, abs=1e-12)
    # Seeded engineering check against the analytic minimum f(2,-1)=0, not a
    # comparative claim or proof of general algorithm quality.
    assert 0 <= result.best.objective_raw < 0.25


def test_exe_004_013_maximization_and_verification(tmp_path):
    math_revision = revision(expression="7-((x1-2)**2+(x2+1)**2)", sense="maximize")
    item = request("main", quota=1000, math_revision=math_revision)
    result, rows = run(item, tmp_path)
    assert result.started_evaluations == 1000
    assert result.best.objective_raw == pytest.approx(7.0, abs=0.005)
    assert all(row["objective_raw"] == -row["score_for_minimization"] for row in rows)
    check, _ = run(request("verification", math_revision=math_revision, job_id=item.job_id, reference=result.best), tmp_path)
    assert check.best is None
    assert check.started_evaluations == 2
    assert check.validation.validation_status == "passed"
    assert check.validation.repeat_objective_raw == [result.best.objective_raw] * 2


@pytest.mark.parametrize("expression", ["1/(x1-x1)", "log(-1)", "exp(1000)"])
def test_dat_008_exe_006_domain_errors_charge_an_fe(tmp_path, expression):
    result, rows = run(request("preflight", math_revision=revision(expression=expression)), tmp_path)
    assert result.execution_status == "failed"
    assert result.error.error_code == "OBJECTIVE_EVALUATION_FAILED"
    assert result.started_evaluations == result.failed_evaluations == 1
    assert result.best is None
    assert rows[0]["objective_raw"] is None


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_exe_006_nonfinite_objective_has_no_fitness(tmp_path, value):
    result, rows = run(request("preflight"), tmp_path, _objective_override=partial(nonfinite, value=value))
    assert result.execution_status == "failed"
    assert result.error.error_code == "OBJECTIVE_EVALUATION_FAILED"
    assert result.started_evaluations == result.failed_evaluations == 1
    assert rows[0]["objective_raw"] is rows[0]["score_for_minimization"] is None


def test_exe_005_objective_fault_preserves_observation(tmp_path):
    counter = mp.get_context("spawn").Value("i", 0)
    result, _ = run(request("preflight"), tmp_path, _objective_override=partial(fails_second, counter=counter))
    assert result.execution_status == "failed"
    assert result.error.error_code == "OBJECTIVE_EVALUATION_FAILED"
    assert result.started_evaluations == counter.value == 2
    assert result.successful_evaluations == result.failed_evaluations == 1
    assert result.best.objective_raw == 1.0


@pytest.mark.parametrize("cancel", [False, True])
def test_exe_009_010_supervisor_reaps_hung_objective(tmp_path, monkeypatch, cancel):
    from numeric_lifecycle_probe import NumericLifecycleProbe

    # Fresh Windows traces reached identity/objective at 1.765--1.812 seconds;
    # 1.8 seconds did not establish this test's running-objective precondition.
    # Ten total seconds include startup and leave an explicit readiness margin.
    item = request("preflight", seconds=10.0)
    with NumericLifecycleProbe(monkeypatch, tmp_path, item) as probe:
        result, rows = probe.execute(lambda **kwargs: run(item, tmp_path, **kwargs), cancel=cancel)
        assert result.execution_status == ("cancelled" if cancel else "timed_out")
        assert not result.history_complete and result.history_sha256 is None
        assert result.started_evaluations == 1
        assert result.successful_evaluations == result.failed_evaluations == 0
        assert not rows and probe.done is None
        assert result.overshoot_seconds < 0.75
        assert result.runtime_provenance["identity"]["pid"] == probe.pid.value
        assert probe.accepted_sequences == [1] and probe.entered.is_set()
        probe.assert_reaped()


@pytest.mark.parametrize("mode", ["blocked_startup", "late_identity_ack"])
def test_exe_009_startup_timeout_keeps_zero_evaluations_and_correct_history(tmp_path, monkeypatch, mode):
    from numeric_lifecycle_probe import NumericLifecycleProbe

    # Both gates consume the unchanged total clock, never a post-handshake clock.
    item = request("preflight", seconds=5.0)
    with NumericLifecycleProbe(monkeypatch, tmp_path, item, mode=mode) as probe:
        result, rows = probe.execute(lambda **kwargs: run(item, tmp_path, **kwargs), require_entry=False)
        assert result.execution_status == "timed_out" and result.termination_reason == "time_budget"
        assert result.started_evaluations == result.successful_evaluations == result.failed_evaluations == 0
        assert probe.accepted_sequences == [] and not probe.entered.is_set() and probe.pid.value == 0
        assert not rows and result.overshoot_seconds < 0.75
        assert result.elapsed_compute_seconds >= item.deadline_seconds
        if mode == "blocked_startup":
            assert probe.startup_entered.is_set() and probe.startup_at.value < probe.deadline
            assert probe.identity is None and probe.done is None
            assert result.runtime_provenance is None and not result.history_complete
            assert result.history_sha256 is None
        else:
            assert probe.identity is not None and probe.done is not None
            assert probe.done["started_evaluations"] == probe.done["successful_evaluations"] == probe.done["failed_evaluations"] == 0
            assert result.history_complete and result.history_sha256 == hashlib.sha256(b"").hexdigest()
            assert result.runtime_provenance["identity"]["pid"] == probe.launched_pid
        probe.assert_reaped()


def test_exe_012_hard_crash_keeps_start_accounting(tmp_path):
    seen = []
    result, rows = run(request("preflight"), tmp_path, _objective_override=dies, on_event=seen.append)
    assert result.execution_status == "unknown_outcome"
    assert result.started_evaluations == 1
    assert seen == [{"kind": "started", "sequence": 1}]
    assert not result.history_complete
    assert not rows


def test_exe_015_stateful_preflight_rejected(tmp_path):
    counter = mp.get_context("spawn").Value("i", 0)
    result, rows = run(request("preflight"), tmp_path, _objective_override=partial(stateful, counter=counter))
    assert result.error.error_code == "DETERMINISM_CHECK_FAILED"
    assert result.started_evaluations == 2
    assert rows[0]["x"] == rows[1]["x"]


def test_exe_016_017_reproducibility_and_profile_scale(tmp_path):
    math_revision = revision(expression="1.7e308")
    items = [request("preflight", math_revision=math_revision), request("preflight", math_revision=math_revision)]
    assert items[0].job_id != items[1].job_id
    assert items[0].seed == items[1].seed
    a, b = [run(item, tmp_path)[1] for item in items]
    assert [row["x"] for row in a] == [row["x"] for row in b]
    assert all(a[index]["x"] == a[index + 1]["x"] for index in range(0, 8, 2))
    profile, _ = run(request("profile", math_revision=math_revision), tmp_path)
    assert profile.started_evaluations == 492
    assert profile.profile_features.score_median == 1.7e308
    assert profile.profile_features.normalized_iqr == profile.profile_features.normalized_std == 0.0


def test_exe_014_019_verification_timeout_and_mismatch(tmp_path):
    item = request("preflight")
    result, _ = run(item, tmp_path)
    timeout, _ = run(
        request("verification", seconds=0.01, math_revision=item.revision, job_id=item.job_id, reference=result.best), tmp_path,
    )
    assert timeout.validation.validation_status == "incomplete"
    mismatch, _ = run(
        request("verification", math_revision=item.revision, job_id=item.job_id, reference=result.best),
        tmp_path, _objective_override=environment_probe,
    )
    assert mismatch.validation.validation_status == "failed"
    assert mismatch.termination_reason == "validation_error"


def test_llm_016_sys_006_child_environment_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("PMCO_TEST_API_KEY", "TEST FIXTURE marker")
    monkeypatch.setenv("PMCO_TEST_INTERNAL_TOKEN", "TEST FIXTURE marker")
    result, _ = run(request("preflight"), tmp_path, _objective_override=environment_probe)
    assert result.execution_status == "completed"
    assert os.environ["PMCO_TEST_INTERNAL_TOKEN"] == "TEST FIXTURE marker"


def test_sys_007_policy_budget():
    for name, count, expected in [
        ("fixed_solver", 0, 19498), ("random_solver", 0, 19498), ("rule_based", 0, 19498),
        ("llm_only", 0, 19498), ("pilot_only", 4, 13498), ("llm_shortlist_then_pilot", 3, 14998),
    ]:
        data = {
            "policy_version": "1.0", "portfolio_id": "continuous_v1", "selection_policy": name,
            "fixed_algorithm_id": "de.original" if name == "fixed_solver" else None,
            "max_objective_evaluations": 20000, "max_compute_seconds": 300, "master_seed": 1405, "explain_via_llm": False,
        }
        policy = ExecutionPolicy.from_input(ExecutionPolicyInput.model_validate(data))
        assert policy.pilot_candidate_count == count
        assert policy.main_evaluations == expected
        with pytest.raises(ValidationError):
            ExecutionPolicyInput.model_validate(data | {"max_objective_evaluations": 1001 + 1500 * count})
        if name in {"fixed_solver", "random_solver", "rule_based", "pilot_only"}:
            assert propose_baseline(policy, revision()) == propose_baseline(policy, revision())


def plan(ids):
    return CandidatePlan(
        candidates=[Candidate(algorithm_id=i, config_id=SOLVER_CONFIG_IDS[i], rationale_fa="TEST FIXTURE", evidence_refs=[]) for i in ids],
        uncertainty_notes=None,
    )


def test_sel_001_002_invalid_plan():
    abc = plan(["abc.original"]).candidates[0]
    with pytest.raises(DomainError):
        validate_plan(plan(["abc.original", "abc.original"]), count=2)
    with pytest.raises(DomainError):
        validate_plan(plan(["abc.original"]), count=3)
    with pytest.raises(DomainError):
        validate_plan(CandidatePlan(candidates=[abc.model_copy(update={"algorithm_id": "arbitrary"})], uncertainty_notes=None), count=1)
    with pytest.raises(ValidationError):
        Candidate.model_validate(abc.model_dump() | {"pop_size": 3})
    with pytest.raises(DomainError):
        validate_plan(CandidatePlan(candidates=[abc.model_copy(update={"config_id": "abc.custom"})], uncertainty_notes=None), count=1)


def fixture_pilots(scores):
    from p_mc_optimizer.domain.models import EvaluationRecord, ExecutionResult

    math_revision, job = revision(), uid()
    pilots = []
    for algorithm, repetitions in scores.items():
        for index, score in enumerate(repetitions):
            trial = trial_request(
                math_revision, job_id=job, trial_id=uid(), attempt_id=uid(), idempotency_key=uid(),
                algorithm_id=algorithm, seed=derive_seed(1405, math_revision.problem_math_hash, "pilot", algorithm, index),
                phase="pilot", replicate_index=index, evaluation_quota=500, deadline_seconds=10.0,
            )
            good = score is not None
            observation = None
            if good:
                observation = EvaluationRecord(
                    job_id=job, execution_id=trial.attempt_id, trial_id=trial.trial_id, attempt_id=trial.attempt_id,
                    stage_execution_id=None, evaluation_id=uid(), sequence=1, phase="pilot", evaluation_kind="library_setup",
                    x=[0.0, 0.0], objective_raw=score, score_for_minimization=score, status="successful",
                    evaluation_seconds=0.0, problem_math_hash=trial.problem_math_hash, evaluator_version="1.0",
                )
            pilots.append((trial, ExecutionResult(
                job_id=job, execution_id=trial.attempt_id, phase="pilot", execution_status="completed" if good else "failed",
                termination_reason="evaluation_budget" if good else "execution_error", best=observation,
                started_evaluations=500 if good else 0, successful_evaluations=500 if good else 0,
                failed_evaluations=0, rejected_proposals=0, elapsed_compute_seconds=1.0, overshoot_seconds=0.0,
                history_complete=True, history_sha256="0" * 64, profile_features=None, validation=None, error=None,
                fingerprint={"fixture": "TEST FIXTURE"},
            )))
    return pilots


def test_sel_003_008_009_median_iqr_id():
    scores = {"abc.original": [-1e308, 0.0, 1e308], "de.original": [-1.0, 0.0, 1.0], "pso.original": [-1.0, 0.0, 1.0]}
    pilots = fixture_pilots(scores)
    original = rank_pilots(pilots, plan(list(scores)))
    reordered = rank_pilots(list(reversed(pilots)), plan(list(reversed(scores))))
    assert original.scale == 1e308
    assert original.selected_algorithm_id == "de.original"
    assert original.metrics == reordered.metrics
    assert original.selected_algorithm_id == reordered.selected_algorithm_id
    assert original.metrics[0].median_score == original.metrics[1].median_score == 0
    assert original.metrics[0].normalized_iqr > original.metrics[1].normalized_iqr
    assert original.metrics[1].normalized_iqr == original.metrics[2].normalized_iqr
    scores["abc.original"][1] = -2.0
    assert rank_pilots(fixture_pilots(scores), plan(list(scores))).selected_algorithm_id == "abc.original"


def test_sel_004_005_require_three_trials_and_two_methods():
    scores = {"abc.original": [None, 0.0, 0.0], "de.original": [3.0, 3.0, 3.0], "pso.original": [4.0, 4.0, 4.0]}
    decision = rank_pilots(fixture_pilots(scores), plan(list(scores)))
    assert decision.selected_algorithm_id == "de.original"
    assert not decision.metrics[0].eligible
    assert len(decision.metrics[0].trial_ids) == 3
    scores["pso.original"][2] = None
    insufficient = rank_pilots(fixture_pilots(scores), plan(list(scores)))
    assert insufficient.selection_status == "insufficient_evidence"
    assert insufficient.selected_algorithm_id is None


def test_atomic_cancel_before_objective_start(tmp_path):
    counter = mp.get_context("spawn").Value("i", 0)
    result, rows = run(
        request("preflight"), tmp_path, _objective_override=partial(spy_objective, counter=counter),
        on_event=lambda event: False if event["kind"] == "started" else None,
    )
    assert result.execution_status == "cancelled"
    assert result.history_complete
    assert result.started_evaluations == counter.value == len(rows) == 0


def test_selection_policy_interfaces_and_recorded_fallback():
    policy = ExecutionPolicy.from_input(ExecutionPolicyInput(
        policy_version="1.0", portfolio_id="continuous_v1", selection_policy="llm_only",
        fixed_algorithm_id=None, max_objective_evaluations=20_000, max_compute_seconds=300,
        master_seed=1405, explain_via_llm=False,
    ))
    implementation = SelectionPolicy()
    context = SelectionContext(policy, revision(), proposal_source="deterministic_fallback")
    proposal = implementation.propose(context)
    assert [item.algorithm_id for item in proposal.candidates] == ["de.original"]
    decision = implementation.decide([], context)
    assert decision.selection_status == "fixed_by_policy"
    assert decision.decision_source == "deterministic_fallback"
    assert decision.selected_algorithm_id == "de.original"


def test_p7_research_manifest_math_and_offline_references():
    examples = Path(__file__).resolve().parents[1] / "examples"
    manifest = strict_json_loads((examples / "benchmark_research.json").read_bytes())
    references = strict_json_loads((examples / manifest["reference_file"]).read_bytes())
    assert {item["dimensions"] for item in manifest["instances"]} == {5, 10, 20}
    assert {item["split"] for item in manifest["instances"]} == {"train", "development", "test"}
    assert len(manifest["master_seeds"]) == 20
    for item in manifest["instances"]:
        problem = ProblemInput.model_validate(strict_json_loads((examples / item["problem"]).read_bytes()))
        assert problem_hashes(problem)[0] == item["problem_math_hash"]
        reference = references[item["instance_id"]]
        objective = objective_for(freeze_problem(problem).spec)
        assert objective(reference["x_star"]) == pytest.approx(reference["f_star"], abs=1e-12)
