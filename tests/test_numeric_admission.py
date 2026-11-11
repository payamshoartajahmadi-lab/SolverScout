"""Actual API/workflow admission and Repository boundaries reject unqualified policies before FE."""
import json
from pathlib import Path
from uuid import uuid4

import pytest

from p_mc_optimizer.catalog import store as catalog_module
from p_mc_optimizer.catalog.models import QualificationReport
from p_mc_optimizer.catalog.qualification import _problem
from p_mc_optimizer.catalog.trials import _request_for_configuration, apply_budget_configuration
from p_mc_optimizer.domain.models import DomainError, ExecutionRequest
from p_mc_optimizer.execution.supervisor import run_execution
from p_mc_optimizer.workflow.catalog_v2 import CatalogWorkflow
from p_mc_optimizer.workflow.worker import run_claimed
from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, checked
from test_llm_workflow import configure, fixture_transport, local_api


_GWO="mealpy:swarm_based.GWO:OriginalGWO"
_IDENTIFIERS={_GWO,"mealpy:swarm_based.WOA:OriginalWOA","mealpy:evolutionary_based.DE:OriginalDE"}


@pytest.fixture
def admission(tmp_path,monkeypatch):
    def controlled_base(config,**kwargs):
        return QualificationReport(config_id=config.config_id,status="qualified",pilot_500_compatible=True,
                                   checks=(),environment=catalog_module.environment_id())
    monkeypatch.setattr(catalog_module,"qualify_configuration",controlled_base)
    calls=[]
    with local_api(tmp_path,fixture_transport([{"ok":True}],calls)) as (repo,client,address,token):
        store=catalog_module.CatalogStore(repo.path)
        result=store.discover()
        ids=[row["config_id"] for row in result["suggested_configurations"] if row["implementation_id"] in _IDENTIFIERS]
        run,_=store.reserve_batch(ids,max_seconds=120,idempotency_key="TEST FIXTURE base admission")
        store.run_batch(run)
        profile,_=configure(client,_PROFILE_SETTINGS,_TEST_KEY)
        yield repo,store,client,address,token,profile,calls


def payload(store,profile,*,fixed=False,problem=None):
    if problem is None:
        problem=json.loads((Path(__file__).parents[1]/"examples/quadratic_2d.json").read_text())
    snapshot=store.snapshot()
    card=next(card for card in snapshot.cards if card.implementation_id==_GWO)
    policy={"policy_version":"2.0","portfolio_id":"continuous_catalog_v2",
            "selection_policy":"catalog_fixed" if fixed else "catalog_llm_then_pilot",
            "master_seed":1405,"max_objective_evaluations":20000,"max_compute_seconds":300,
            "explain_via_llm":False}
    if fixed:
        policy.update(fixed_implementation_id=card.implementation_id,fixed_config_id=card.config_id)
    return {"schema_version":"2.0","input":{"kind":"structured_problem","problem":problem},
            "policy":policy,"llm_profile":None if fixed else {key:profile[key] for key in ("profile_id","revision")},
            "catalog_hash":snapshot.catalog_hash}


def test_fixed_admission_rejects_missing_policy_certificate_without_creating_job(admission):
    repo,store,client,address,token,profile,calls=admission
    error=checked(client.post("/jobs",json=payload(store,profile,fixed=True),headers={"Idempotency-Key":str(uuid4())}),422)
    assert error["error_code"]=="NUMERIC_POLICY_QUALIFICATION_REQUIRED"
    assert repo.list_jobs()==[]
    with repo.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM stage_executions").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM trial_attempts").fetchone()[0]==0
    assert calls==["connection_test_v1"]


def test_automatic_workflow_excludes_uncertified_cards_before_first_fe(admission):
    repo,store,client,address,token,profile,calls=admission
    job_id=checked(client.post("/jobs",json=payload(store,profile),headers={"Idempotency-Key":str(uuid4())}),202)["job_id"]
    owner=str(uuid4())
    claimed=repo.claim_job(owner,job_id=job_id,lease_seconds=1200)
    run_claimed(repo,claimed,owner,api_url=address,token=token)
    state=checked(client.get(f"/jobs/{job_id}"))
    assert state["job_status"]=="failed"
    assert state["error"]["error_code"]=="CATALOG_CANDIDATES_INSUFFICIENT"
    assert state["budget"]["known_started"]==state["budget"]["reserved_active"]==state["budget"]["charged_unknown"]==0
    assert repo.list_executions(job_id)==[]
    snapshot=repo.get_v2_snapshot(job_id)["numeric_execution"]
    assert len(snapshot["exclusions"])==2
    assert all(row["error_code"]=="NUMERIC_POLICY_QUALIFICATION_REQUIRED" for row in snapshot["exclusions"])
    assert calls==["connection_test_v1"]


def test_job_repository_and_runner_reject_qualification_authority_kind_without_any_fe(admission,tmp_path):
    repo,store,client,address,token,profile,calls=admission
    problem=_problem(5,"q07").spec.model_dump(mode="json",exclude={"source_map"})
    job_id=checked(client.post("/jobs",json=payload(store,profile,problem=problem),headers={"Idempotency-Key":str(uuid4())}),202)["job_id"]
    owner=str(uuid4())
    assert repo.claim_job(owner,job_id=job_id,lease_seconds=1200)
    workflow=CatalogWorkflow(repo,job_id,owner,api_url=address,token=token)
    workflow.freeze_problem()
    revision=workflow.revision
    catalog=store.snapshot()
    card=next(card for card in catalog.cards if card.implementation_id==_GWO)
    execution=str(uuid4())
    trial=_request_for_configuration(revision,configuration=card.configuration,catalog_hash=catalog.catalog_hash,
        job_id=job_id,trial_id=str(uuid4()),attempt_id=execution,idempotency_key=execution,seed=1405,
        phase="main",replicate_index=0,evaluation_quota=14998,deadline_seconds=60.0)
    trial=apply_budget_configuration(trial,repo.get_v2_snapshot(job_id)["numeric_execution"],
                                    {"kind":"qualification_run","qualification_id":str(uuid4()),
                                     "check_id":"shifted_5d","certificate_hash":None})
    with pytest.raises(DomainError) as error:
        repo.start_execution(job_id,"main",14998,60.0,trial_request=trial,worker_id=owner)
    assert error.value.code=="NUMERIC_POLICY_AUTHORIZATION_INVALID"
    assert repo.list_executions(job_id)==[]
    request=ExecutionRequest(job_id=job_id,execution_id=execution,phase="main",revision=revision,seed=1405,
                             evaluation_quota=14998,deadline_seconds=60.0,trial=trial)
    path=tmp_path/"unstarted"/"history.jsonl"
    with pytest.raises(DomainError) as error:
        run_execution(request,history_path=path)
    assert error.value.code=="NUMERIC_POLICY_AUTHORIZATION_INVALID"
    assert not path.parent.exists()
    assert repo.get_job(job_id)["budget"]["known_started"]==0
    assert calls==["connection_test_v1"]
