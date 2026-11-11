"""Budgeted stop accounting and legacy unmarked catalog-job compatibility."""
from functools import partial
import json
import multiprocessing as mp
import os
from pathlib import Path
from uuid import uuid4

import pytest

from p_mc_optimizer.catalog.adapter import resolve_configuration
from p_mc_optimizer.catalog.configuration import canonical_json
from p_mc_optimizer.catalog.qualification import _problem
from p_mc_optimizer.catalog.store import CatalogStore
from p_mc_optimizer.domain.models import ExecutionRequest, TrialRequestBudgetV1, TrialRequestV2
from p_mc_optimizer.execution.supervisor import run_execution
from p_mc_optimizer.selection.policies import derive_seed
from p_mc_optimizer.workflow.catalog_v2 import CatalogWorkflow
from test_llm_gateway import checked
from test_llm_workflow import local_api
from test_numeric import hang

GWO = "mealpy:swarm_based.GWO:OriginalGWO"
DE = "mealpy:evolutionary_based.DE:OriginalDE"


def evidence(name, data):
    output = os.environ.get("PMC_QA_COMPATIBILITY_EVIDENCE_DIR")
    if output:
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        (destination/(name+".json")).write_text(
            json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)+"\n",
        )


class GenuineCatalog:
    def __init__(self, components):
        self.components = components

    def __iter__(self):
        return iter(self.components)

    def __repr__(self):
        return "GenuineCatalog(isolated_local_api)"


@pytest.fixture(scope="module")
def genuine_catalog(tmp_path_factory):
    data = tmp_path_factory.mktemp("numeric-compatibility")
    with local_api(data, None) as (repo, client, address, token):
        store = CatalogStore(repo.path)
        store.discover()
        ids = [resolve_configuration(name).config_id for name in (GWO, DE)]
        base_id, _ = store.reserve_batch(ids, max_seconds=120, idempotency_key="compatibility-base")
        store.run_batch(base_id)
        budget_id, _ = store.reserve_batch(
            [resolve_configuration(GWO).config_id], max_seconds=300,
            scope="budget_horizon.v1", idempotency_key="compatibility-budget",
        )
        store.run_batch(budget_id)
        snapshot = store.numeric_execution_snapshot(store.snapshot())
        assert resolve_configuration(GWO).config_id in snapshot["certificates"]
        assert store.snapshot().get(DE, resolve_configuration(DE).config_id).qualified_report.status == "qualified"
        evidence("qualifications", {"base":store.batch(base_id), "budget":store.batch(budget_id)})
        yield GenuineCatalog((repo, store, client, address, token))


def make_fixed(genuine_catalog, implementation, *, legacy=False):
    repo, store, client, address, token = genuine_catalog
    card = store.snapshot().get(implementation, resolve_configuration(implementation).config_id)
    payload = {
        "schema_version":"2.0",
        "input":{"kind":"structured_problem",
                 "problem":_problem(2,"sphere").spec.model_dump(mode="json", exclude={"source_map"})},
        "policy":{
            "policy_version":"2.0", "portfolio_id":"continuous_catalog_v2",
            "selection_policy":"catalog_fixed", "fixed_implementation_id":implementation,
            "fixed_config_id":card.config_id, "master_seed":1405,
            "max_objective_evaluations":1504, "max_compute_seconds":300, "explain_via_llm":False,
        },
        "llm_profile":None, "catalog_hash":store.snapshot().catalog_hash,
    }
    job_id = checked(client.post("/jobs",json=payload,headers={"Idempotency-Key":str(uuid4())}),202)["job_id"]
    if legacy:
        # Recreate the persisted pre-upgrade shape before any execution is reserved.
        # Product load/build/reserve/run must preserve this shape.
        with repo.transaction() as connection:
            snapshot = json.loads(connection.execute(
                "SELECT payload FROM job_v2_snapshots WHERE job_id=?", (job_id,),
            ).fetchone()[0])
            snapshot.pop("numeric_execution")
            connection.execute("UPDATE job_v2_snapshots SET payload=? WHERE job_id=?",
                               (canonical_json(snapshot),job_id))
            workflow = json.loads(connection.execute("SELECT workflow FROM jobs WHERE job_id=?",(job_id,)).fetchone()[0])
            workflow.pop("execution_contracts")
            connection.execute("UPDATE jobs SET workflow=? WHERE job_id=?",(canonical_json(workflow),job_id))
    owner = str(uuid4())
    assert repo.claim_job(owner,job_id=job_id,lease_seconds=1200)
    workflow = CatalogWorkflow(repo,job_id,owner,api_url=address,token=token)
    workflow.freeze_problem()
    assert workflow.prepare_candidates() is None
    workflow.propose_candidates()
    return workflow, card


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout","cancel"])
def test_genuine_budget_trial_stop_preserves_durable_known_unknown_unused(genuine_catalog, tmp_path, cancel):
    repo, store, client, address, token = genuine_catalog
    workflow, card = make_fixed(genuine_catalog,GWO)
    revision = workflow.revision
    execution_id = str(uuid4())
    seed = derive_seed(1405,revision.problem_math_hash,"main",card.seed_identity,0)
    quota, deadline = 1002, 3.0
    trial = workflow.build_trial(
        revision,algorithm_id=GWO,job_id=workflow.job_id,trial_id=str(uuid4()),
        attempt_id=execution_id,idempotency_key=execution_id,seed=seed,phase="main",
        replicate_index=0,evaluation_quota=quota,deadline_seconds=deadline,
    )
    assert isinstance(trial,TrialRequestBudgetV1)
    assert trial.numeric_execution.authorization.kind == "job_certificate"
    row = repo.start_execution(
        workflow.job_id,"main",quota,deadline,trial_request=trial,
        execution_id=execution_id,worker_id=workflow.worker_id,
    )
    assert row["created"]
    request = ExecutionRequest(
        job_id=workflow.job_id,execution_id=execution_id,phase="main",revision=revision,
        seed=seed,evaluation_quota=quota,deadline_seconds=deadline,trial=trial,
    )
    process_id = mp.get_context("spawn").Value("i",0)
    requested_cancel = []

    def should_cancel():
        if cancel and process_id.value and not requested_cancel:
            state = checked(client.get(f"/jobs/{workflow.job_id}"))
            checked(client.post(
                f"/jobs/{workflow.job_id}/cancel",json={"expected_job_version":state["version"]},
            ),202)
            requested_cancel.append(True)
        return bool(requested_cancel)

    def started(event):
        if event["kind"] == "started":
            return repo.record_started(execution_id,sequence=event["sequence"],worker_id=workflow.worker_id)

    result = run_execution(
        request,history_path=tmp_path/"history.jsonl",
        _objective_override=partial(hang,process_id=process_id),cancelled=should_cancel,on_event=started,
    )
    assert result.execution_status == ("cancelled" if cancel else "timed_out")
    assert result.termination_reason == ("cancelled" if cancel else "time_budget")
    assert result.started_evaluations == 1 and result.successful_evaluations == result.failed_evaluations == 0
    assert not result.history_complete and result.best is None
    assert process_id.value > 0
    repo.finish_execution(execution_id,result,worker_id=workflow.worker_id)
    state = checked(client.get(f"/jobs/{workflow.job_id}"))
    ledger = state["budget"]
    assert ledger["allocated"] == quota
    assert ledger["known_started"] == 1
    assert ledger["charged_unknown"] == ledger["reserved_active"] == 0
    assert ledger["unused"] == quota - 1
    assert ledger["unallocated"] == 502
    assert sum(ledger[key] for key in ("known_started","charged_unknown","reserved_active","unused")) == ledger["allocated"]
    with repo.connect() as database:
        assert database.execute("SELECT COUNT(*) FROM llm_calls WHERE job_id=?",(workflow.job_id,)).fetchone()[0] == 0
    evidence("budget-cancel" if cancel else "budget-timeout", {
        "requirement":"NUM-15","status":"PASS","job_id":workflow.job_id,
        "request":request.model_dump(mode="json"),"outcome":result.model_dump(mode="json"),"ledger":ledger,
        "api_model_calls":0,
    })
    workflow.keep("stop", {"status":"cancelled" if cancel else "failed",
                           "reason":"cancelled" if cancel else "time_budget", "error":None})
    workflow.build_report_data()
    workflow.explain_result()
    workflow.finalize_job()


def test_old_catalog_job_without_markers_remains_legacy_before_first_trial(genuine_catalog):
    repo, store, client, address, token = genuine_catalog
    workflow, card = make_fixed(genuine_catalog,DE,legacy=True)
    assert repo.list_executions(workflow.job_id) == []
    assert workflow.numeric_execution is None
    assert "execution_contracts" not in workflow.workflow
    with repo.connect() as database:
        frozen_before = database.execute(
            "SELECT payload FROM job_v2_snapshots WHERE job_id=?",(workflow.job_id,),
        ).fetchone()[0]
    revision = workflow.revision
    execution_id = str(uuid4())
    trial = workflow.build_trial(
        revision,algorithm_id=DE,job_id=workflow.job_id,trial_id=str(uuid4()),attempt_id=execution_id,
        idempotency_key=execution_id,seed=1405,phase="main",replicate_index=0,
        evaluation_quota=workflow.policy.main_evaluations,deadline_seconds=60.0,
    )
    assert type(trial) is TrialRequestV2
    assert trial.schema_version == "2.0" and "numeric_execution" not in trial.model_dump(mode="json")
    result = workflow._execute("main",workflow.policy.main_evaluations,algorithm_id=DE)
    assert result.execution_status == "completed" and result.history_complete
    assert result.started_evaluations == workflow.policy.main_evaluations == 1002
    rows = repo.list_executions(workflow.job_id)
    assert len(rows) == 1
    saved_trial = rows[0]["request"]
    assert saved_trial["schema_version"] == "2.0"
    assert "numeric_execution" not in saved_trial
    assert saved_trial["configuration_snapshot"] == card.configuration.kwargs()
    assert rows[0]["known_started"] == 1002 and rows[0]["charged_unknown"] == rows[0]["reserved_active"] == 0
    assert "execution_contracts" not in workflow.workflow and workflow.numeric_execution is None
    with repo.connect() as database:
        frozen_after = database.execute(
            "SELECT payload FROM job_v2_snapshots WHERE job_id=?",(workflow.job_id,),
        ).fetchone()[0]
        assert database.execute("SELECT COUNT(*) FROM llm_calls WHERE job_id=?",(workflow.job_id,)).fetchone()[0] == 0
    assert frozen_after == frozen_before
    evidence("legacy-unmarked-catalog-job", {
        "requirement":"CP-01","status":"PASS","job_id":workflow.job_id,
        "before_first_trial":{"numeric_execution":None,"execution_contracts_present":False},
        "snapshot_unchanged":frozen_before==frozen_after,"saved_trial":saved_trial,
        "outcome":result.model_dump(mode="json"),"ledger":repo.get_job(workflow.job_id)["budget"],"api_model_calls":0,
    })
