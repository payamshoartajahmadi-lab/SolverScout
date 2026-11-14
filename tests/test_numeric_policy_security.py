"""Targeted integrity regressions discovered during the final numeric contract audit."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from p_mc_optimizer.catalog.qualification import _numeric_history
from p_mc_optimizer.domain.models import EvaluationRecord
from p_mc_optimizer.workflow.catalog_v2 import CatalogWorkflow


@pytest.fixture
def recorded_numeric_history(tmp_path):
    job_id, execution_id, trial_id = (str(uuid4()) for _ in range(3))
    best = EvaluationRecord(
        job_id=job_id, execution_id=execution_id, trial_id=trial_id, attempt_id=execution_id,
        stage_execution_id=None, evaluation_id=str(uuid4()), sequence=1, phase="pilot",
        evaluation_kind="library_setup", x=[0.0,0.0], objective_raw=0.0,score_for_minimization=0.0,
        status="successful",evaluation_seconds=0.0,problem_math_hash="fixture",evaluator_version="1.0",
    )
    rows = [best.model_copy(update={"sequence": index+1,"evaluation_id":str(uuid4()),
                                   "evaluation_kind":"library_setup" if index==0 else "objective"})
            for index in range(500)]
    best = rows[0]
    body = b"".join((row.model_dump_json()+"\n").encode() for row in rows)
    path = tmp_path / "history.jsonl"
    path.write_bytes(body)
    request = SimpleNamespace(
        evaluation_quota=500,
        revision=SimpleNamespace(spec=SimpleNamespace(variables=[SimpleNamespace(lower=-5.0,upper=5.0)]*2)),
        trial=SimpleNamespace(numeric_execution=SimpleNamespace(
            derivation=SimpleNamespace(expected_started_without_error=500),
            authorization=SimpleNamespace(kind="qualification_run",check_id="sphere_2d"))),
    )
    result = SimpleNamespace(history_complete=True,rejected_proposals=0,history_sha256=hashlib.sha256(body).hexdigest(),
                             started_evaluations=500,successful_evaluations=500,failed_evaluations=0,
                             execution_status="completed",termination_reason="evaluation_budget",error=None,best=best)
    return result,request,path


@pytest.mark.parametrize("change", [{"x":[2.0,2.0]}, {"evaluation_id":"00000000-0000-4000-8000-000000000000"},
                                    {"execution_id":"00000000-0000-4000-8000-000000000000"}])
def test_policy_certificate_rejects_best_record_not_present_in_history(recorded_numeric_history, change):
    result,request,path = recorded_numeric_history
    assert _numeric_history(result,request,path)[0]
    result.best = result.best.model_copy(update=change)
    assert not _numeric_history(result,request,path)[0]


@pytest.mark.parametrize("change", ["fingerprint","policy"])
def test_frozen_job_mismatch_is_rejected_before_candidate_or_preflight_work(change):
    from p_mc_optimizer.catalog.budget_policy import POLICY_DEFINITION, POLICY_HASH, numeric_runtime_fingerprint_hash

    snapshot = {"policy_definition":deepcopy(POLICY_DEFINITION),"policy_hash":POLICY_HASH,
                "numeric_runtime_fingerprint_sha256":numeric_runtime_fingerprint_hash()}
    if change == "fingerprint":
        snapshot["numeric_runtime_fingerprint_sha256"]="0"*64
        expected="NUMERIC_POLICY_ENVIRONMENT_MISMATCH"
    else:
        snapshot["policy_definition"]["setup_evaluations"]=2
        expected="NUMERIC_POLICY_INVALID"
    captured=[]

    class FrozenWorkflow(CatalogWorkflow):
        @property
        def numeric_execution(self):
            return snapshot

        @property
        def selection_policy(self):
            pytest.fail("A mismatched snapshot advanced to policy/candidate work")

        @property
        def revision(self):
            pytest.fail("A mismatched snapshot advanced to problem/candidate work")

        def stop(self, code, message):
            captured.append(code)
            return {"route":"report"}

    workflow=FrozenWorkflow(None,"fixture","fixture")
    assert workflow.prepare_candidates()=={"route":"report"}
    assert captured==[expected]
    pipeline=workflow.pipeline()
    assert pipeline.index("prepare_candidates") < pipeline.index("preflight")


from test_numeric_policy_qualification import base_fixture


@pytest.mark.parametrize("change", ["matrix", "profile", "policy"])
def test_qualification_definition_drift_rejects_before_check_reservation(base_fixture, monkeypatch, change):
    from p_mc_optimizer.catalog import store as module
    from p_mc_optimizer.catalog import budget_policy
    from p_mc_optimizer.catalog.models import SolverConfiguration
    from p_mc_optimizer.catalog.qualification import numeric_check_request
    from p_mc_optimizer.domain.models import DomainError

    store, ids, _ = base_fixture
    run, _ = store.reserve_batch(ids[:1],max_seconds=300,idempotency_key="TEST FIXTURE definition drift",scope="budget_horizon.v1")
    with store._connect() as db:
        original=db.execute("SELECT numeric_policy_snapshot FROM qualification_runs WHERE qualification_id=?",(run,)).fetchone()[0]
    snapshot=json.loads(original)
    config=SolverConfiguration.model_validate_json(json.dumps(snapshot["base_configurations"][ids[0]]))
    entry=snapshot["check_matrix"][0]
    request=numeric_check_request(config,entry,snapshot,run,remaining=60)
    if change=="matrix":
        matrix=deepcopy(snapshot["check_matrix"])
        matrix[1]["evaluation_quota"]=501
        monkeypatch.setattr(module,"numeric_check_matrix",lambda:matrix)
    elif change=="profile":
        snapshot["qualification_profile_id"]="other_profile"
        with store._connect(write=True) as db:
            db.execute("UPDATE qualification_runs SET numeric_policy_snapshot=? WHERE qualification_id=?",(json.dumps(snapshot),run))
    else:
        changed=deepcopy(budget_policy.POLICY_DEFINITION)
        changed["setup_evaluations"]=2
        monkeypatch.setattr(budget_policy,"POLICY_DEFINITION",changed)
    with pytest.raises(DomainError) as error:
        store._reserve_numeric_check(run,config,snapshot,entry,request)
    assert error.value.code=="NUMERIC_POLICY_ENVIRONMENT_MISMATCH"
    observed=store.batch(run)
    assert observed["reserved_evaluations"]==observed["reserved_active"]==observed["charged_unknown"]==0


def test_cancelled_or_still_running_run_never_authorizes_a_published_certificate(base_fixture):
    from p_mc_optimizer.catalog.models import digest

    store,ids,_=base_fixture
    run,_=store.reserve_batch(ids[:1],max_seconds=300,idempotency_key="TEST FIXTURE late cancellation",scope="budget_horizon.v1")
    snapshot=store.numeric_execution_snapshot(store.snapshot())
    payload={"status":"qualified","qualification_id":run,"pilot_500_compatible":True,"fixture":"publication race"}
    with store._connect(write=True) as db:
        db.execute("""INSERT INTO numeric_policy_qualification_reports
                   (qualification_id,config_id,policy_hash,environment,report_hash,payload,created_at)
                   VALUES (?,?,?,?,?,?,?)""",(run,ids[0],snapshot["policy_hash"],snapshot["numeric_runtime_fingerprint_sha256"],
                                             digest(payload),json.dumps(payload),"2099-01-01"))
    assert not store.numeric_execution_snapshot(store.snapshot())["certificates"]
    with store._connect(write=True) as db:
        db.execute("UPDATE qualification_runs SET status='cancelled',cancel_requested=1 WHERE qualification_id=?",(run,))
    assert not store.numeric_execution_snapshot(store.snapshot())["certificates"]


@pytest.mark.parametrize("field", ["qualification_id", "base_config_id", "implementation_id", "policy_id", "policy_hash",
                                  "numeric_runtime_fingerprint_sha256", "numeric_runtime_fingerprint", "pilot_500_compatible"])
def test_snapshot_never_admits_self_consistent_but_crosslinked_certificate(base_fixture, field):
    from p_mc_optimizer.catalog.models import digest

    store,ids,_=base_fixture
    run,_=store.reserve_batch(ids[:1],max_seconds=300,idempotency_key="TEST FIXTURE certificate binding",scope="budget_horizon.v1")
    catalog=store.snapshot()
    snapshot=store.numeric_execution_snapshot(catalog)
    card=next(card for card in catalog.cards if card.config_id==ids[0])
    payload={"schema_version":"1.0","status":"qualified","qualification_id":run,
             "base_config_id":ids[0],"implementation_id":card.implementation_id,"policy_id":snapshot["policy_id"],
             "policy_hash":snapshot["policy_hash"],"numeric_runtime_fingerprint":snapshot["numeric_runtime_fingerprint"],
             "numeric_runtime_fingerprint_sha256":snapshot["numeric_runtime_fingerprint_sha256"],"pilot_500_compatible":True}
    if field=="numeric_runtime_fingerprint":
        payload[field]=deepcopy(payload[field])
        payload[field]["numpy_version"]="different"
    elif field=="pilot_500_compatible":
        payload[field]=False
    else:
        payload[field]="0"*64 if field.endswith("hash") or field.endswith("id") else "different"
    with store._connect(write=True) as db:
        db.execute("UPDATE qualification_runs SET status='completed' WHERE qualification_id=?",(run,))
        db.execute("""INSERT INTO numeric_policy_qualification_reports
                   (qualification_id,config_id,policy_hash,environment,report_hash,payload,created_at)
                   VALUES (?,?,?,?,?,?,?)""",(run,ids[0],snapshot["policy_hash"],snapshot["numeric_runtime_fingerprint_sha256"],
                                             digest(payload),json.dumps(payload),"2099-01-01"))
    frozen=store.numeric_execution_snapshot(catalog)
    assert ids[0] not in frozen["certificates"]
    assert any(row["base_config_id"]==ids[0] and row["error_code"]=="NUMERIC_POLICY_QUALIFICATION_REQUIRED"
               for row in frozen["exclusions"])
