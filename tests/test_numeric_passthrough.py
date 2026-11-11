"""Real base-qualified DE keeps the identical trajectory under budget passthrough."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from p_mc_optimizer.catalog.adapter import resolve_configuration, seed_identity
from p_mc_optimizer.catalog.budget_policy import validate_job_numeric_trial
from p_mc_optimizer.catalog.configuration import digest
from p_mc_optimizer.catalog.qualification import _problem
from p_mc_optimizer.catalog.store import CATALOG_SCHEMA, CatalogStore
from p_mc_optimizer.catalog.trials import _request_for_configuration, apply_budget_configuration
from p_mc_optimizer.domain.models import ExecutionRequest
from p_mc_optimizer.execution.supervisor import run_execution
from p_mc_optimizer.selection.policies import derive_seed


def test_real_base_qualified_passthrough_preserves_full_legacy_numeric_trace(tmp_path, monkeypatch):
    from p_mc_optimizer.llm import provider

    calls = []

    def forbid_provider(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Numeric compatibility does not require a language-model call")

    monkeypatch.setattr(provider, "call_provider", forbid_provider)
    database = tmp_path/"catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(CATALOG_SCHEMA)
    store = CatalogStore(database)
    store.discover()
    implementation = "mealpy:evolutionary_based.DE:OriginalDE"
    configuration = resolve_configuration(implementation)
    qualification_id, _ = store.reserve_batch(
        [configuration.config_id], max_seconds=120,
        idempotency_key="passthrough-real-base-qualification",
    )
    store.run_batch(qualification_id)
    base_report = store.batch(qualification_id)
    assert base_report["status"] == "completed"
    catalog = store.snapshot()
    card = catalog.get(implementation, configuration.config_id)
    assert card.qualified_report.status == "qualified"

    numeric_snapshot = store.numeric_execution_snapshot(catalog)
    assert numeric_snapshot["certificates"] == {}
    assert numeric_snapshot["exclusions"] == []
    qualification = card.qualified_report.model_dump(mode="json")
    authorization = {
        "kind":"base_catalog", "base_config_id":configuration.config_id,
        "catalog_hash":catalog.catalog_hash, "qualified_report":qualification,
        "qualified_report_hash":digest(qualification),
    }
    revision = _problem(2, "sphere")
    seed = derive_seed(1405, revision.problem_math_hash, "main", seed_identity(implementation), 0)
    quota = 1002
    histories, results, trials = {}, {}, {}

    for policy in ("legacy_static.v1", "budget_horizon.v1"):
        execution_id = str(uuid4())
        trial = _request_for_configuration(
            revision, configuration=configuration, catalog_hash=catalog.catalog_hash,
            job_id=str(uuid4()), trial_id=str(uuid4()), attempt_id=execution_id,
            idempotency_key=execution_id, seed=seed, phase="main", replicate_index=0,
            evaluation_quota=quota, deadline_seconds=60,
        )
        if policy == "budget_horizon.v1":
            trial = apply_budget_configuration(trial, numeric_snapshot, authorization)
            validate_job_numeric_trial(trial.model_dump(mode="json"), numeric_snapshot, catalog.model_dump(mode="json"))
            numeric = trial.numeric_execution
            assert numeric.application == "passthrough"
            assert numeric.reason == "not_allowlisted" and numeric.derivation is None
            assert numeric.effective_configuration == configuration
            assert numeric.effective_config_hash == configuration.config_id
            assert numeric.authorization.kind == "base_catalog"
        else:
            validate_job_numeric_trial(trial.model_dump(mode="json"), None, catalog.model_dump(mode="json"))

        request = ExecutionRequest(
            job_id=trial.job_id, execution_id=execution_id, phase="main", revision=revision,
            seed=seed, evaluation_quota=quota, deadline_seconds=60, trial=trial,
        )
        history = tmp_path/(policy+".jsonl")
        result = run_execution(request, history_path=history)
        assert result.execution_status == "completed", result.error
        assert result.termination_reason == "evaluation_budget"
        assert result.started_evaluations == result.successful_evaluations == quota
        assert result.history_complete and result.rejected_proposals == result.failed_evaluations == 0
        assert hashlib.sha256(history.read_bytes()).hexdigest() == result.history_sha256
        records = [json.loads(line) for line in history.read_text().splitlines()]
        assert len(records) == quota and records[0]["evaluation_kind"] == "library_setup"
        assert all(math.isclose(row["objective_raw"], sum(x*x for x in row["x"]),
                                abs_tol=1e-12, rel_tol=1e-10) for row in records)
        assert all(all(-5 <= x <= 5 for x in row["x"]) for row in records)
        histories[policy] = [(row["x"], row["objective_raw"], row["evaluation_kind"]) for row in records]
        results[policy] = result
        trials[policy] = trial

    assert histories["legacy_static.v1"] == histories["budget_horizon.v1"]
    legacy, budget = results.values()
    assert legacy.best.x == budget.best.x
    assert legacy.best.objective_raw == budget.best.objective_raw
    assert trials["legacy_static.v1"].configuration_snapshot == trials["budget_horizon.v1"].configuration_snapshot
    assert trials["legacy_static.v1"].seed == trials["budget_horizon.v1"].seed == seed
    assert not calls

    output = os.environ.get("PMC_QA_PASSTHROUGH_EVIDENCE_DIR")
    if output:
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        for policy in histories:
            shutil.copyfile(tmp_path/(policy+".jsonl"), destination/(policy+".jsonl"))
        evidence = {
            "requirements":["NUM-06","CP-11"], "status":"PASS", "implementation_id":implementation,
            "master_seed":1405, "trial_seed":seed, "evaluation_quota":quota,
            "api_calls":len(calls), "base_qualification":base_report,
            "numeric_snapshot":numeric_snapshot,
            "base_configuration":configuration.model_dump(mode="json"),
            "trials":{key:value.model_dump(mode="json") for key,value in trials.items()},
            "outcomes":{key:value.model_dump(mode="json") for key,value in results.items()},
            "full_numeric_tuples":histories,
            "trace_equal":True,
        }
        (destination/"passthrough-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, allow_nan=False, indent=2)+"\n",
        )
