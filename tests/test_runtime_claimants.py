"""A resumed job retains identities from both real claimant processes."""
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from p_mc_optimizer.application.jobs import ApplicationService
from p_mc_optimizer.domain.models import CreateJob
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.runtime_provenance import report_runtime_provenance


def test_new_claimant_adds_identity_without_replacing_previous_producer(tmp_path):
    repo = Repository(tmp_path)
    problem = json.loads((Path(__file__).parents[1] / "examples/quadratic_2d.json").read_text())
    request = CreateJob.model_validate({
        "input": {"kind": "structured_problem", "problem": problem},
        "policy": {"policy_version": "1.0", "portfolio_id": "continuous_v1", "selection_policy": "fixed_solver",
                   "fixed_algorithm_id": "de.original", "master_seed": 1405, "max_objective_evaluations": 1002,
                   "max_compute_seconds": 30, "explain_via_llm": False},
        "llm_profile": None,
    })
    job_id = ApplicationService(repo).create_job(request, str(uuid4()))["job_id"]
    script = '''
import json,sys
from uuid import uuid4
from p_mc_optimizer.runtime_provenance import startup
from p_mc_optimizer.persistence import Repository
startup("worker",data_dir=sys.argv[1])
repo=Repository(sys.argv[1])
owner=str(uuid4())
job=repo.claim_job(owner,job_id=sys.argv[2])
print(json.dumps(job["workflow"]["runtime:workers"]))
'''
    def claim():
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path), job_id],
                                check=True, capture_output=True, text=True, timeout=20)
        return json.loads(result.stdout)

    first = claim()
    original_id = next(iter(first))
    repo.recover_expired_jobs(force=True)
    second = claim()
    assert len(second) == 2
    assert second[original_id] == first[original_id]
    identities = [entry["identity"] for entry in second.values()]
    assert len({identity["pid"] for identity in identities}) == 2
    assert all(identity["role"] == "worker" for identity in identities)
    projected = report_runtime_provenance(repo, job_id)
    assert {identity["process_run_id"] for identity in identities} <= {
        identity["process_run_id"] for identity in projected["processes"]
    }
    assert not repo.list_executions(job_id)
