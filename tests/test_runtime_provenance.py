"""Read-only runtime diagnostics and real process identities from isolated source copies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from p_mc_optimizer import runtime_provenance
from p_mc_optimizer.api import create_app
from p_mc_optimizer.persistence import Repository
from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, checked, target


@pytest.fixture
def source_copy(tmp_path):
    root = tmp_path / "copy"
    package = root / "p_mc_optimizer"
    shutil.copytree(runtime_provenance.PACKAGE_ROOT, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return root


def process(root, script, *args):
    env = {**os.environ, "PYTHONPATH": str(root)}
    result = subprocess.run([sys.executable, "-c", script, *map(str,args)], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_runtime_identity_is_stable_in_process_and_new_after_restart(source_copy, tmp_path):
    script = '''
import json,sys
from p_mc_optimizer.runtime_provenance import startup,runtime_payload,save_runtime
first=startup("worker",data_dir=sys.argv[1])
second=startup("worker",data_dir=sys.argv[1])
payload=runtime_payload()
print(json.dumps({"first":first,"second":second,"payload":payload}))
'''
    one = process(source_copy, script, tmp_path / "data")
    two = process(source_copy, script, tmp_path / "data")
    assert one["first"] == one["second"]
    assert two["first"] == two["second"]
    assert one["first"]["process_run_id"] != two["first"]["process_run_id"]
    assert one["first"]["pid"] != two["first"]["pid"]
    saved = list((tmp_path / "data/runtime/provenance").glob("*.json"))
    assert len(saved) == 2
    assert sorted(json.loads(path.read_text())["identity"]["process_run_id"] for path in saved) == sorted([
        one["first"]["process_run_id"], two["first"]["process_run_id"],
    ])


def test_source_copy_has_unknown_git_and_real_manifest_then_detects_disk_drift(source_copy):
    script = '''
import json
from pathlib import Path
from p_mc_optimizer.runtime_provenance import startup,runtime_payload,runtime_status,PACKAGE_ROOT
from p_mc_optimizer.catalog.budget_policy import numeric_runtime_fingerprint_hash
first=startup("worker")
fingerprint=numeric_runtime_fingerprint_hash()
path=PACKAGE_ROOT/"execution/evaluator.py"
path.write_text(path.read_text()+"\\n# TEST FIXTURE changed after startup\\n")
second=startup("worker")
print(json.dumps({"first":first,"second":second,"status":runtime_status(),
                  "numeric_before":fingerprint,"numeric_after":numeric_runtime_fingerprint_hash(),
                  "manifest":runtime_payload()["source_manifest"]}))
'''
    output = process(source_copy, script)
    assert output["first"] == output["second"]
    assert output["first"]["git_commit_observed_at_startup"] is None
    assert output["first"]["git_dirty_observed_at_startup"] is None
    assert output["first"]["source_manifest_status"] == "complete"
    assert output["status"]["drift"] == "changed"
    assert output["status"]["message_code"] == "RUNTIME_RESTART_REQUIRED"
    assert output["numeric_before"] == output["numeric_after"]
    assert all(set(row) == {"path", "sha256"} and not Path(row["path"]).is_absolute() for row in output["manifest"])


def test_git_observation_records_dirty_checkout_without_fabricating_commit(source_copy):
    subprocess.run(["git", "init", "-q"], cwd=source_copy, check=True)
    subprocess.run(["git", "add", "p_mc_optimizer"], cwd=source_copy, check=True)
    subprocess.run(["git", "-c", "user.name=QA Fixture", "-c", "user.email=fixture@example.test",
                    "commit", "-qm", "TEST FIXTURE source"], cwd=source_copy, check=True)
    expected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source_copy, text=True).strip()
    path = source_copy / "p_mc_optimizer/llm/prompts/explainer.v1.txt"
    path.write_text(path.read_text() + "\nTEST FIXTURE local edit\n")
    identity = process(source_copy, 'import json; from p_mc_optimizer.runtime_provenance import startup; print(json.dumps(startup("api")))')
    assert identity["git_commit_observed_at_startup"] == expected
    assert identity["git_dirty_observed_at_startup"] is True


def test_prompt_bundle_is_frozen_until_new_process(source_copy):
    script = '''
import json
from p_mc_optimizer.llm.provider import freeze_prompt_bundle,_prompt,PROMPT_DIR
from p_mc_optimizer.runtime_provenance import startup,runtime_status
startup("api")
one=freeze_prompt_bundle()["explainer.v2"]
path=PROMPT_DIR/"explainer.v2.txt"
path.write_text("TEST FIXTURE replacement prompt")
print(json.dumps({"before":one,"after":_prompt("explainer.v2"),"drift":runtime_status()["drift"]}))
'''
    output = process(source_copy, script)
    assert output["before"] == output["after"]
    assert output["after"] != "TEST FIXTURE replacement prompt"
    assert output["drift"] == "changed"
    fresh = process(source_copy, 'import json; from p_mc_optimizer.llm.provider import _prompt; print(json.dumps(_prompt("explainer.v2")))')
    assert fresh == "TEST FIXTURE replacement prompt"


def test_runtime_endpoint_and_doctor_preserve_session_credential_and_jobs(tmp_path):
    token = "TEST_FIXTURE_INTERNAL_TOKEN"
    calls = []

    def unexpected(request):
        calls.append(request)
        raise AssertionError("Runtime diagnostics contacted the model provider")

    import httpx
    app = create_app(tmp_path, token, transport=httpx.MockTransport(unexpected))
    with TestClient(app, headers={"X-PMC-Internal-Token":token}) as client:
        profile = checked(client.put("/settings/llm/profile", json={**_PROFILE_SETTINGS,"expected_revision":None}))
        checked(client.put("/settings/llm/credential", json={**target(profile),"api_key":_TEST_KEY,"storage_mode":"session"}))
        before = checked(client.get("/settings/llm/profile"))
        problem = json.loads((Path(__file__).parents[1] / "examples/quadratic_2d.json").read_text())
        payload = {"input":{"kind":"structured_problem","problem":problem},"llm_profile":None,
                   "policy":{"policy_version":"1.0","portfolio_id":"continuous_v1","selection_policy":"fixed_solver",
                             "fixed_algorithm_id":"de.original","master_seed":1405,"max_objective_evaluations":1002,
                             "max_compute_seconds":120,"explain_via_llm":False}}
        job_ids = [checked(client.post("/jobs",json=payload,headers={"Idempotency-Key":str(uuid4())}),202)["job_id"]
                   for _ in range(2)]
        creators = [app.state.repo.get_job(job_id)["workflow"]["runtime:creator"] for job_id in job_ids]
        assert creators[0]["process_run_id"] == creators[1]["process_run_id"]
        with app.state.repo.connect() as db:
            initial = [db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("jobs", "llm_calls")]
        output = checked(client.get("/runtime"))
        assert output["identity"]["process_run_id"]
        assert _TEST_KEY not in json.dumps(output) and token not in json.dumps(output)
        assert checked(client.get("/settings/llm/profile")) == before
        subprocess.run([sys.executable,"-m","p_mc_optimizer","doctor","--data-dir",str(tmp_path)],
                       capture_output=True,text=True,check=True,timeout=30)
        with app.state.repo.connect() as db:
            assert [db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("jobs", "llm_calls")] == initial
        assert checked(client.get("/settings/llm/profile")) == before
    assert calls == []


def test_legacy_report_has_unknown_runtime_without_rewriting_history(tmp_path):
    repo = Repository(tmp_path)
    job_id = str(uuid4())
    payload = '{"schema_version":"1.0","fixture":"legacy immutable"}'
    with repo.connect() as db:
        db.execute("""INSERT INTO jobs(job_id,input,policy,job_status,phase,workflow,created_at,updated_at)
                   VALUES (?, '{}','{"max_objective_evaluations":20000}','completed','reporting','{}','a','a')""",(job_id,))
        db.execute("INSERT INTO reports VALUES (?, ?, '[]', 'a')", (job_id,payload))
    provenance = runtime_provenance.report_runtime_provenance(repo,job_id)
    assert provenance == {"status":"legacy_unknown","processes":[]}
    with repo.connect() as db:
        assert db.execute("SELECT payload FROM reports WHERE job_id=?",(job_id,)).fetchone()[0] == payload
        assert db.execute("SELECT workflow FROM jobs WHERE job_id=?",(job_id,)).fetchone()[0] == '{}'
