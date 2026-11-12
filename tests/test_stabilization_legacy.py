"""Persisted requests and idempotent jobs keep their original contracts."""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

from p_mc_optimizer.application.jobs import ApplicationService
from p_mc_optimizer.domain.models import CreateJob, TrialRequestV2
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.persistence.repository import json_text


def test_legacy_trial_serialization_keeps_exact_canonical_shape():
    raw = json.loads((Path(__file__).parent / "fixtures/stabilization_legacy_trial.json").read_text())
    parsed = TrialRequestV2.model_validate_json(json.dumps(raw))
    assert parsed.model_dump(mode="json") == raw
    assert "numeric_execution" not in parsed.model_dump(mode="json")
    assert json_text(parsed.model_dump(mode="json")) == json_text(raw)


def test_legacy_job_create_replay_keeps_missing_markers_and_custom_workflow(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    service = ApplicationService(repo)
    problem = json.loads((Path(__file__).parents[1] / "examples/quadratic_2d.json").read_text())
    request = CreateJob.model_validate({
        "input": {"kind": "structured_problem", "problem": problem},
        "policy": {
            "policy_version": "1.0", "portfolio_id": "continuous_v1", "selection_policy": "fixed_solver",
            "fixed_algorithm_id": "de.original", "master_seed": 1405, "max_objective_evaluations": 1002,
            "max_compute_seconds": 30, "explain_via_llm": False,
        },
        "llm_profile": None,
    })
    key = str(uuid4())
    created = service.create_job(request,key)
    assert created["workflow"]["execution_contracts"]["explanation"] == "grounded_narrative_v3"
    with repo.transaction() as connection:
        repo.write_job(connection,created["job_id"],{"workflow":{"persisted_legacy_field":{"value":True}}})
    before = deepcopy(repo.get_job(created["job_id"])["workflow"])
    from p_mc_optimizer import runtime_provenance
    monkeypatch.setattr(runtime_provenance,"get_runtime",lambda *a,**kw: (_ for _ in ()).throw(AssertionError("Replay rebuilt runtime contracts")))
    replay = service.create_job(request,key)
    assert replay["job_id"] == created["job_id"]
    assert replay["workflow"] == before
    assert "execution_contracts" not in replay["workflow"]
