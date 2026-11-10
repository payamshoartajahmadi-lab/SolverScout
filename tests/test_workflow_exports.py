"""TEST FIXTURE numerical integration and offline export contracts."""

import json
from pathlib import Path

from p_mc_optimizer.__main__ import solve
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.reporting import add_progress, csv_bytes, html_bytes

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_maximize_job_verifies_and_exports_from_same_engine(tmp_path):
    """FLOW-09. A fixed-solver job produces no model calls or Explainer snapshot."""
    repo = Repository(tmp_path / "داده آزمون")
    model = json.loads((EXAMPLES / "quadratic_2d_max.json").read_text())
    policy = {
        "policy_version": "1.0", "portfolio_id": "continuous_v1", "selection_policy": "fixed_solver",
        "fixed_algorithm_id": "de.original", "max_objective_evaluations": 1002, "max_compute_seconds": 300,
        "master_seed": 1405, "explain_via_llm": False,
    }
    output = solve(repo, model, policy)
    report = output["report"]
    assert output["complete"] and report["solution_status"] == "valid_best_found"
    assert report["optimality_status"] == "not_certified"
    assert 6.99 < report["objective_raw"] <= 7
    assert report["objective_raw"] == -report["score_for_minimization"]
    assert report["verification"]["repeat_objective_raw"] == [report["objective_raw"]] * 2
    assert report["verification"]["reference_evaluation_id"] == report["solution_origin"]["evaluation_id"]
    assert report["budget"]["known_started"] == report["progress"][-1]["started_evaluations"] == 1002
    assert [row["known_started"] for row in repo.list_executions(report["job_id"])] == [8, 492, 500, 2]
    assert report["llm_calls"] == [] and report["explanation_status"] == "not_requested"
    assert "role_context:explainer" not in repo.get_job(report["job_id"])["workflow"]
    files = {artifact["kind"]: repo.artifact_path(artifact["artifact_id"]) for artifact in output["artifacts"]}
    assert json.loads(files["report_json"].read_bytes()) == report
    html = files["report_html"].read_text()
    assert "<svg" in html and "https://" not in html and "<script" not in html
    assert "de.original" in files["trials_csv"].read_text()


def test_offline_render_escapes_untrusted_text_and_preserves_negative_numbers():
    unsafe = "<script>alert(1)</script>"
    report = {"input": {"text": unsafe}, "explanation": {"summary_fa": unsafe}, "progress": [
        {"started_evaluations": 1, "score_for_minimization": 1.7e308},
        {"started_evaluations": 2, "score_for_minimization": -1.7e308},
    ]}
    result = html_bytes(report).decode()
    assert unsafe not in result and "&lt;script&gt;" in result
    assert "nan," not in result.lower() and "inf," not in result.lower()
    data = csv_bytes([{"label": "  =cmd", "raw": -3.0}], ("label", "raw")).decode()
    assert "'  =cmd,-3.0" in data


def test_progress_respects_global_started_counters_and_excludes_verification():
    points = add_progress([
        ([{"sequence": 2, "evaluation_id": "a", "score_for_minimization": 5.0}], 3, "profile"),
        ([{"sequence": 2, "evaluation_id": "b", "score_for_minimization": 3.0}], 3, "main"),
        ([{"sequence": 1, "evaluation_id": "c", "score_for_minimization": 1.0}], 2, "verification"),
    ])
    assert [point["started_evaluations"] for point in points] == [2, 5, 8]
    assert [point["score_for_minimization"] for point in points] == [5.0, 3.0, 3.0]


def _test_policy(kind):
    return {
        "policy_version": "1.0", "portfolio_id": "continuous_v1", "selection_policy": kind,
        "fixed_algorithm_id": "de.original" if kind == "fixed_solver" else None,
        "max_objective_evaluations": 1002 if kind == "fixed_solver" else 7002, "max_compute_seconds": 300,
        "master_seed": 1405, "explain_via_llm": False,
    }


def _inject_trial_failure(monkeypatch, phase):
    import hashlib

    from p_mc_optimizer.domain.models import ExecutionError, ExecutionResult
    from p_mc_optimizer.workflow import graph

    original = graph.run_execution

    def execute(request, *, history_path, **kwargs):
        if request.phase != phase:
            return original(request, history_path=history_path, **kwargs)
        history_path.write_bytes(b"")
        return ExecutionResult(
            job_id=request.job_id, execution_id=request.execution_id, phase=request.phase,
            execution_status="failed", termination_reason="execution_error", best=None,
            started_evaluations=0, successful_evaluations=0, failed_evaluations=0, rejected_proposals=0,
            elapsed_compute_seconds=0.001, overshoot_seconds=0.0, history_complete=True,
            history_sha256=hashlib.sha256(b"").hexdigest(), profile_features=None, validation=None,
            error=ExecutionError(error_code="SOLVER_EXECUTION_FAILED", message_fa="TEST FIXTURE Solver failure"),
            fingerprint={"fixture": "TEST FIXTURE"},
        )

    monkeypatch.setattr(graph, "run_execution", execute)


def test_exe_018_dat_013_main_failure_preserves_and_verifies_profile_origin(tmp_path, monkeypatch):
    _inject_trial_failure(monkeypatch, "main")
    repo = Repository(tmp_path)
    problem = json.loads((EXAMPLES / "quadratic_2d.json").read_bytes())
    output = solve(repo, problem, _test_policy("fixed_solver"))
    report = output["report"]
    assert report["job_status"] == "completed"
    assert report["partial_result"] and not report["main_success"]
    assert report["solution_status"] == "valid_best_found"
    assert report["error"]["error_code"] == "SOLVER_EXECUTION_FAILED"
    assert report["selected_algorithm_id"] == "de.original"
    assert report["solution_origin"]["phase"] == "profile"
    assert report["solution_origin"]["algorithm_id"] is None
    assert report["verification"]["reference_evaluation_id"] == report["solution_origin"]["evaluation_id"]
    assert report["budget"]["known_started"] == 502


def test_sel_005_007_failed_pilots_are_not_retried_or_reallocated(tmp_path, monkeypatch):
    _inject_trial_failure(monkeypatch, "pilot")
    repo = Repository(tmp_path)
    problem = json.loads((EXAMPLES / "quadratic_2d.json").read_bytes())
    report = solve(repo, problem, _test_policy("pilot_only"))["report"]
    assert report["job_status"] == "completed"
    assert report["selection_status"] == "insufficient_evidence"
    assert report["selected_algorithm_id"] is None
    assert report["partial_result"] and report["solution_status"] == "valid_best_found"
    assert report["budget"]["known_started"] == 502
    assert len(report["trials"]) == 12
    assert all(row["phase"] == "pilot" and row["evaluation_quota"] == 500 for row in report["trials"])
    assert all(not row["eligible"] for row in report["decision"]["metrics"])
    assert report["llm_calls"] == [] and report["explanation_status"] == "not_requested"


def test_objective_failure_publishes_diagnostic_and_charges_failed_call(tmp_path):
    repo = Repository(tmp_path)
    model = json.loads((EXAMPLES / "quadratic_2d.json").read_bytes())
    model["objective"]["source"]["expression"] = "log(-1)"
    result = solve(repo, model, _test_policy("fixed_solver"))["report"]
    assert result["job_status"] == "failed"
    assert result["error"]["error_code"] == "OBJECTIVE_EVALUATION_FAILED"
    assert result["budget"]["known_started"] == 1
    with repo.connect() as connection:
        artifact_id = connection.execute("SELECT artifact_id FROM artifacts WHERE kind='numeric_diagnostic'").fetchone()[0]
    assert "ValueError: math domain error" in repo.artifact_path(artifact_id).read_text(encoding="utf-8")


def test_canonical_sample_and_baseline_rejection_before_external_call(tmp_path):
    from copy import deepcopy
    from uuid import uuid4

    from fastapi.testclient import TestClient
    from p_mc_optimizer.api import create_app
    from p_mc_optimizer.domain.models import ProblemInput

    example = json.loads((EXAMPLES / "quadratic_2d.json").read_text())
    for variable, label in zip(example["variables"], ("متغیر اول", "متغیر دوم"), strict=True):
        variable["label_fa"] = label
    ProblemInput.model_validate(example)
    for variable in example["variables"]:
        variable["label_fa"] = None
    request = {"input": {"kind": "structured_problem", "problem": example},
               "policy": {"policy_version": "1.0", "portfolio_id": "continuous_v1",
                          "selection_policy": "fixed_solver", "fixed_algorithm_id": "de.original",
                          "max_objective_evaluations": 20000, "max_compute_seconds": 300,
                          "master_seed": 1405, "explain_via_llm": False}, "llm_profile": None}

    def no_provider(_):
        raise AssertionError("Unexpected provider request")

    import httpx

    token = str(uuid4())
    with TestClient(create_app(tmp_path, token, transport=httpx.MockTransport(no_provider))) as client:
        def submit(body):
            return client.post("/jobs", json=body, headers={"X-PMC-Internal-Token": token,
                                                           "Idempotency-Key": str(uuid4())})
        assert submit(request).status_code == 202
        bad = deepcopy(request)
        bad["input"] = {"kind": "natural_language", "text": "تابع را کمینه کن."}
        assert submit(bad).status_code == 422
        bad = deepcopy(request)
        bad["llm_profile"] = {"profile_id": str(uuid4()), "revision": 1}
        assert submit(bad).status_code == 422
        bad = deepcopy(request)
        bad["input"]["problem"]["unknown_field"] = True
        problem = submit(bad).json()
        assert problem["error_code"] == "INVALID_SCHEMA"
        assert any(e["code"] == "extra_forbidden" for e in problem["field_errors"])
