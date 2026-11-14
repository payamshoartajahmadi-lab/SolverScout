"""Budget-horizon contracts and production subprocess execution."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from p_mc_optimizer.catalog.adapter import resolve_configuration
from p_mc_optimizer.catalog.budget_models import NumericExecutionV1
from p_mc_optimizer.catalog.budget_policy import (
    ALLOWLIST, POLICY_DEFINITION, POLICY_HASH, _NumericQualificationAuthority,
    derive_effective_configuration, numeric_runtime_fingerprint, numeric_runtime_fingerprint_hash,
)
from p_mc_optimizer.catalog.configuration import digest
from p_mc_optimizer.catalog.qualification import numeric_check_matrix, numeric_check_request
from p_mc_optimizer.catalog.trials import _request_for_configuration, apply_budget_configuration
from p_mc_optimizer.domain.models import DomainError, ExecutionRequest, TrialRequestBudgetV1, parse_catalog_trial
from p_mc_optimizer.execution.supervisor import run_execution


def frozen_policy():
    fingerprint = numeric_runtime_fingerprint()
    return {
        "schema_version":"1.0", "policy_id":"budget_horizon.v1",
        "policy_definition":deepcopy(POLICY_DEFINITION),"policy_hash":POLICY_HASH,
        "numeric_runtime_fingerprint":fingerprint,"numeric_runtime_fingerprint_sha256":digest(fingerprint),
    }


def derive(config, quota):
    return derive_effective_configuration(
        config,quota,POLICY_DEFINITION,POLICY_HASH,numeric_runtime_fingerprint()
    )


def authority(request):
    numeric=request.trial.numeric_execution
    auth=numeric.authorization
    return _NumericQualificationAuthority(
        qualification_id=auth.qualification_id,check_id=auth.check_id,execution_id=request.execution_id,
        base_config_id=request.trial.config_id,policy_hash=numeric.policy_hash,
        numeric_runtime_fingerprint_sha256=numeric.numeric_runtime_fingerprint_sha256,
        evaluation_quota=request.evaluation_quota,phase=request.phase,request_hash=digest(request.model_dump(mode="json")),
    )


@pytest.mark.parametrize(("quota","epoch","natural","unused"),[
    (102,1,201,0),(500,4,501,0),(501,4,501,0),(14998,149,15001,0),
])
def test_budget_formula_boundaries(quota,epoch,natural,unused):
    config=resolve_configuration(ALLOWLIST[0])
    result=derive(config,quota)
    assert result["derivation"]["effective_epoch"]==epoch
    assert result["derivation"]["natural_evaluations"]==natural
    assert result["derivation"]["expected_started_without_error"]==quota
    assert result["derivation"]["expected_unused_without_error"]==unused
    assert result["effective_configuration"].kwargs()=={**config.kwargs(),"epoch":epoch}


def test_insufficient_initial_population_and_capped_epoch():
    with pytest.raises(DomainError) as error:
        derive(resolve_configuration(ALLOWLIST[0]),101)
    assert error.value.code=="NUMERIC_POLICY_BUDGET_TOO_SMALL"
    result=derive(resolve_configuration(ALLOWLIST[0],{"epoch":2}),500)
    assert result["reason"]=="base_epoch_cap"
    assert result["derivation"]["expected_started_without_error"]==301
    assert result["derivation"]["expected_unused_without_error"]==199


@pytest.mark.parametrize(("name","value"),[
    ("epoch",True),("epoch",2.0),("epoch",0),("epoch",100001),
    ("pop_size",True),("pop_size",100.0),("pop_size",4),("pop_size",10001),
])
def test_budget_invalid_constructor_types_and_limits(name,value):
    config=resolve_configuration(ALLOWLIST[0],{name:value})
    with pytest.raises(DomainError) as error:
        derive(config,500)
    assert error.value.code=="NUMERIC_POLICY_INVALID"


def test_policy_fingerprint_passthrough_and_hash_contracts():
    config=resolve_configuration("mealpy:evolutionary_based.DE:OriginalDE")
    result=derive(config,500)
    assert result["application"]=="passthrough" and result["reason"]=="not_allowlisted"
    assert result["effective_configuration"]==config and result["derivation"] is None
    gwo=resolve_configuration(ALLOWLIST[0])
    assert derive(gwo,500)["effective_config_hash"]==derive(gwo,501)["effective_config_hash"]
    assert POLICY_HASH==digest(POLICY_DEFINITION)
    fp=numeric_runtime_fingerprint()
    assert digest(fp)==numeric_runtime_fingerprint_hash()
    fp["numpy_version"]="changed"
    assert numeric_runtime_fingerprint()["numpy_version"]!="changed"
    for changed in (gwo.model_copy(update={"library_version":"other"}),):
        with pytest.raises(DomainError) as error:
            derive(changed,500)
        assert error.value.code=="NUMERIC_POLICY_ENVIRONMENT_MISMATCH"


def test_policy_definition_tampering_is_rejected():
    changed=deepcopy(POLICY_DEFINITION)
    changed["setup_evaluations"]=2
    with pytest.raises(DomainError) as error:
        derive_effective_configuration(resolve_configuration(ALLOWLIST[0]),500,changed,digest(changed),numeric_runtime_fingerprint())
    assert error.value.code=="NUMERIC_POLICY_INVALID"


@pytest.mark.parametrize(("check","epoch","expected","status"),[
    ("sphere_2d",10000,500,"completed"),("sphere_2d",2,301,"completed"),
    ("controlled_failure",10000,1,"failed"),
])
def test_trusted_qualification_subprocess_counts_and_history(tmp_path,check,epoch,expected,status):
    config=resolve_configuration(ALLOWLIST[0],{"epoch":epoch})
    entry=next(row for row in numeric_check_matrix() if row["check_id"]==check)
    request=numeric_check_request(config,entry,frozen_policy(),str(uuid4()),remaining=60)
    path=tmp_path/"history.jsonl"
    identities=[]
    result=run_execution(request,history_path=path,_qualification_authority=authority(request),on_identity=identities.append)
    assert result.execution_status==status, result.error
    assert result.started_evaluations==expected
    assert result.history_complete
    assert hashlib.sha256(path.read_bytes()).hexdigest()==result.history_sha256
    records=[json.loads(line) for line in path.read_text().splitlines()]
    assert len(records)==expected and records[0]["evaluation_kind"]=="library_setup"
    assert all(all(-5 <= x <= 5 for x in row["x"]) for row in records)
    assert identities==[result.runtime_provenance]
    if check=="controlled_failure":
        assert result.successful_evaluations==0 and result.error.error_code=="OBJECTIVE_EVALUATION_FAILED"
    elif expected==301:
        assert result.termination_reason=="solver_stop"
    else:
        assert result.termination_reason=="evaluation_budget"
    assert parse_catalog_trial(request.trial.model_dump(mode="json"))==request.trial


@pytest.mark.parametrize("fault",["missing","dict","hash","quota","check","effective","fingerprint"])
def test_authority_and_envelope_rejected_before_history(tmp_path,fault):
    config=resolve_configuration(ALLOWLIST[0])
    request=numeric_check_request(config,numeric_check_matrix()[0],frozen_policy(),str(uuid4()),remaining=60)
    allowed=authority(request)
    if fault=="missing":
        allowed=None
    elif fault=="dict":
        allowed=allowed.__dict__
    elif fault=="hash":
        allowed=replace(allowed,request_hash="0"*64)
    elif fault=="quota":
        allowed=replace(allowed,evaluation_quota=102)
    elif fault=="check":
        allowed=replace(allowed,check_id="not-a-check")
    elif fault=="effective":
        altered=request.trial.numeric_execution.model_copy(update={"derivation":request.trial.numeric_execution.derivation.model_copy(update={"effective_epoch":5})})
        request=request.model_copy(update={"trial":request.trial.model_copy(update={"numeric_execution":altered})})
        allowed=authority(request)
    elif fault=="fingerprint":
        altered=request.trial.numeric_execution.model_copy(update={"numeric_runtime_fingerprint_sha256":"0"*64})
        request=request.model_copy(update={"trial":request.trial.model_copy(update={"numeric_execution":altered})})
    path=tmp_path/"new-directory"/"history.jsonl"
    with pytest.raises(DomainError):
        run_execution(request,history_path=path,_qualification_authority=allowed)
    assert not path.parent.exists()


def build_qualified_store(directory):
    """Obtain genuine base and budget-policy certificates in the current environment."""
    from p_mc_optimizer.catalog.store import CATALOG_SCHEMA,CatalogStore
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/"catalog.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(CATALOG_SCHEMA)
    store=CatalogStore(path)
    store.discover()
    configurations=[resolve_configuration(identifier) for identifier in ALLOWLIST]
    config_ids=[config.config_id for config in configurations]
    base, _ = store.reserve_batch(config_ids,max_seconds=120,idempotency_key="budget-fixture-base")
    store.run_batch(base)
    policy, _ = store.reserve_batch(config_ids,max_seconds=300,idempotency_key="budget-fixture-policy",scope="budget_horizon.v1")
    store.run_batch(policy)
    snapshot=store.numeric_execution_snapshot(store.snapshot())
    assert all(config_id in snapshot["certificates"] for config_id in config_ids),store.batch(policy)
    return store


@pytest.fixture(scope="session")
def budget_qualified_store(tmp_path_factory):
    return build_qualified_store(tmp_path_factory.mktemp("budget-qualified"))


def authorized_request(store,implementation,quota,seed=1152613831):
    from p_mc_optimizer.catalog.qualification import _problem
    revision=_problem(5,"q07")
    config=resolve_configuration(implementation)
    catalog=store.snapshot()
    snapshot=store.numeric_execution_snapshot(catalog)
    wrapper=snapshot["certificates"][config.config_id]
    certificate=wrapper["certificate"]
    permission={"kind":"job_certificate","qualification_id":certificate["qualification_id"],
                "certificate_hash":wrapper["certificate_hash"],"certificate":certificate}
    identity=str(uuid4())
    trial=_request_for_configuration(
        revision,configuration=config,catalog_hash=catalog.catalog_hash,job_id=str(uuid4()),trial_id=str(uuid4()),
        attempt_id=identity,idempotency_key=identity,seed=seed,phase="main",replicate_index=0,
        evaluation_quota=quota,deadline_seconds=60,
    )
    return ExecutionRequest(
        job_id=trial.job_id,execution_id=identity,phase="main",revision=revision,seed=seed,
        evaluation_quota=quota,deadline_seconds=60,trial=apply_budget_configuration(trial,snapshot,permission),
    )


@pytest.mark.parametrize("quota",[102,501])
def test_real_certificate_partial_and_exact_generation_budget(tmp_path,budget_qualified_store,quota):
    request=authorized_request(budget_qualified_store,ALLOWLIST[0],quota)
    result=run_execution(request,history_path=tmp_path/"history.jsonl")
    assert result.execution_status=="completed" and result.termination_reason=="evaluation_budget",result.error
    assert result.started_evaluations==quota and result.history_complete


def test_q07_legacy_and_budget_policy_reference_twice(tmp_path,budget_qualified_store):
    from p_mc_optimizer.domain.models import TrialRequestV2
    request=authorized_request(budget_qualified_store,ALLOWLIST[0],14998)
    assert request.revision.problem_math_hash=="9f22cd5c17fa014e07fa6a13410b09af5d1f08221f2a7672955e1e516fb87e04"
    assert request.revision.evaluation_contract_hash=="7d7960c1d23429e38a4519546b375dbccccec627f2e85898a5556e6ba2be9675"
    histories={}
    for policy in ("legacy","budget"):
        for replicate in range(2):
            trial=request.trial
            if policy=="legacy":
                raw=trial.model_dump(mode="json")
                raw.pop("numeric_execution")
                raw["schema_version"]="2.0"
                trial=TrialRequestV2.model_validate(raw)
            current=request.model_copy(update={"trial":trial})
            path=tmp_path/f"{policy}-{replicate}.jsonl"
            result=run_execution(current,history_path=path)
            assert result.execution_status=="completed" and result.started_evaluations==14998
            assert result.termination_reason=="evaluation_budget" and result.history_complete
            expected=.037474207549668154 if policy=="legacy" else .00001705662406997132
            assert result.best.objective_raw==pytest.approx(expected,rel=1e-8,abs=1e-10)
            if policy=="budget":
                assert result.best.objective_raw <= 1e-4
            records=[json.loads(line) for line in path.read_text().splitlines()]
            histories[policy,replicate]=[(row["x"],row["objective_raw"],row["evaluation_kind"]) for row in records]
            assert records[0]["evaluation_kind"]=="library_setup"
            assert hashlib.sha256(path.read_bytes()).hexdigest()==result.history_sha256
        assert histories[policy,0]==histories[policy,1]


def test_trial_policy_quota_and_replicate_hashes():
    config = resolve_configuration(ALLOWLIST[0])
    base_entry = numeric_check_matrix()[0]
    request = numeric_check_request(config, base_entry, frozen_policy(), str(uuid4()), remaining=60)
    with pytest.raises(ValueError):
        TrialRequestBudgetV1.model_validate({
            **request.trial.model_dump(mode="python"),
            "numeric_execution": request.trial.numeric_execution.model_copy(update={"evaluation_quota":501}),
        })
    assert request.trial.config_id == request.trial.numeric_execution.base_config_id == config.config_id
    baseline = derive(config,500)
    assert all(derive(config,500)["effective_config_hash"] == baseline["effective_config_hash"] for _ in range(3))
    assert derive(config,14998)["effective_config_hash"] != baseline["effective_config_hash"]


@pytest.mark.parametrize("field", ["base_config_id", "policy_hash", "numeric_runtime_fingerprint_sha256"])
def test_unrelated_certificate_rejected_before_execution(tmp_path, budget_qualified_store, field):
    from p_mc_optimizer.catalog.budget_models import JobCertificateAuthorization
    request = authorized_request(budget_qualified_store, ALLOWLIST[0], 102)
    numeric = request.trial.numeric_execution
    certificate = {**numeric.authorization.certificate, field:"0"*64}
    authorization = JobCertificateAuthorization(
        kind="job_certificate", qualification_id=numeric.authorization.qualification_id,
        certificate_hash=digest(certificate), certificate=certificate,
    )
    altered = numeric.model_copy(update={"authorization":authorization})
    request = request.model_copy(update={"trial":request.trial.model_copy(update={"numeric_execution":altered})})
    path = tmp_path/"untouched"/"history.jsonl"
    with pytest.raises(DomainError) as error:
        run_execution(request,history_path=path)
    assert error.value.code == "NUMERIC_POLICY_AUTHORIZATION_INVALID"
    assert not path.parent.exists()


def test_authority_does_not_authorize_legacy_or_unknown_checks(tmp_path):
    from p_mc_optimizer.domain.models import TrialRequestV2
    config = resolve_configuration(ALLOWLIST[0])
    request = numeric_check_request(config,numeric_check_matrix()[0],frozen_policy(),str(uuid4()),remaining=60)
    legacy_shape = request.trial.model_dump(mode="json")
    legacy_shape.pop("numeric_execution")
    legacy_shape["schema_version"] = "2.0"
    legacy = request.model_copy(update={"trial":TrialRequestV2.model_validate(legacy_shape)})
    with pytest.raises(DomainError) as error:
        run_execution(legacy,history_path=tmp_path/"legacy.jsonl",_qualification_authority=authority(request))
    assert error.value.code == "NUMERIC_POLICY_AUTHORIZATION_INVALID"
    numeric = request.trial.numeric_execution
    permission = numeric.authorization.model_copy(update={"check_id":"unknown-check"})
    request = request.model_copy(update={"trial":request.trial.model_copy(
        update={"numeric_execution":numeric.model_copy(update={"authorization":permission})})})
    with pytest.raises(DomainError) as error:
        run_execution(request,history_path=tmp_path/"unknown.jsonl",_qualification_authority=authority(request))
    assert error.value.code == "NUMERIC_POLICY_AUTHORIZATION_INVALID"
    assert not tuple(tmp_path.iterdir())
