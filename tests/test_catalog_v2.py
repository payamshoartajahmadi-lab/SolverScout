"""Catalog discovery, qualification, retrieval and isolated numeric acceptance."""

from __future__ import annotations

import json
import platform
import sqlite3
import tracemalloc
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.catalog import CATALOG_SCHEMA, CatalogSnapshot, CatalogStore, SolverCardV2, catalog_trial_request, shortlist
from p_mc_optimizer.catalog import adapter, store as storage
from p_mc_optimizer.catalog.models import Parameter, QualificationReport, SolverConfiguration, cards_hash
from p_mc_optimizer.catalog.qualification import QUALIFICATION_CASES, QUALIFICATION_FE_PER_CONFIG, _problem
from p_mc_optimizer.domain.models import DomainError, ExecutionRequest, TrialRequest, TrialRequestV2
from p_mc_optimizer.execution.supervisor import run_execution
from p_mc_optimizer.reporting import json_bytes
from p_mc_optimizer.selection.policies import derive_seed

_GWO = "mealpy:swarm_based.GWO:OriginalGWO"
_WOA = "mealpy:swarm_based.WOA:OriginalWOA"


def _store(tmp_path: Path) -> CatalogStore:
    db = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(CATALOG_SCHEMA)
    return CatalogStore(db)


def test_cat_001_discovery_is_inventory_only(tmp_path, monkeypatch):
    from mealpy import get_all_optimizers

    expected = set(get_all_optimizers(verbose=False).values())

    def forbidden(*args, **kwargs):
        pytest.fail("Discovery instantiated an optimizer")

    for cls in expected:
        monkeypatch.setattr(cls, "__init__", forbidden)
    store = _store(tmp_path)
    result = store.discover()
    assert result["discovered_count"] == len(expected)
    assert result["qualified_count"] == 0
    assert store.get_snapshot(result["catalog_hash"]).cards == ()
    assert len(store.list_implementations(limit=17, offset=10)["items"]) == 17


def test_cat_002_opaque_constructor_needs_configuration():
    class Opaque:
        def __init__(self, *args, **kwargs):
            pytest.fail("Discovery instantiated an opaque optimizer")

    class CallableDefault:
        def __init__(self, epoch=1, pop_size=30, callback=lambda: None):
            pytest.fail("Discovery instantiated an optimizer with a callable default")

    assert adapter._constructor_parameters(Opaque, {}) is None
    assert adapter._constructor_parameters(CallableDefault, {}) is None


def test_cat_003_hash_changes_and_v1_schema_unchanged():
    original = adapter.resolve_configuration(_GWO)
    changed = adapter.resolve_configuration(_GWO, {"pop_size": 30})
    assert original.config_id != changed.config_id
    for key in ("library_version", "adapter_version", "boundary_repair"):
        assert original.model_copy(update={key: "changed"}).config_id != original.config_id
    for contract, filename in [(TrialRequest, "trial_request.v1.json"), (TrialRequestV2, "trial_request.v2.json")]:
        path = Path(__file__).resolve().parents[1] / "schemas" / filename
        assert path.read_bytes() == json_bytes(contract.model_json_schema()) + b"\n"


@pytest.fixture(scope="module")
def qualified(tmp_path_factory):
    store = _store(tmp_path_factory.mktemp("catalog-qualification"))
    discovery = store.discover()
    candidates = [row for row in discovery["suggested_configurations"] if row["implementation_id"] in {_GWO, _WOA}]
    ids = [candidate["config_id"] for candidate in candidates]
    qualification_id, created = store.reserve_batch(ids, max_seconds=80, idempotency_key="test-qualification")
    assert created
    assert store.reserve_batch(ids[::-1], max_seconds=80, idempotency_key="test-qualification") == (qualification_id, False)
    with pytest.raises(ApiError) as conflict:
        store.reserve_batch(ids, max_seconds=81, idempotency_key="test-qualification")
    assert conflict.value.error_code == "IDEMPOTENCY_CONFLICT"
    store.run_batch(qualification_id)
    return store, qualification_id, discovery["catalog_hash"]


def test_cat_005_num_001_006_qualification_uses_real_subprocesses(qualified):
    store, qualification_id, old_hash = qualified
    batch = store.batch(qualification_id)
    assert batch["status"] == "completed"
    assert batch["max_evaluations"] == 2 * QUALIFICATION_FE_PER_CONFIG
    assert batch["checks_completed"] == 2 * len(QUALIFICATION_CASES)
    assert batch["charged_unknown"] == 0
    assert batch["started_evaluations"] == 5002
    assert all(report["status"] == "qualified" for report in batch["reports"])
    assert all(check["history_sha256"] for report in batch["reports"] for check in report["checks"])
    snapshot = store.snapshot()
    assert set(card.implementation_id for card in snapshot.cards) == {_GWO, _WOA}
    assert snapshot.catalog_hash != old_hash
    assert store.get_snapshot(old_hash).cards == ()
    assert store.get_snapshot(snapshot.catalog_hash) == snapshot


def test_cat_003_004_version_and_qualification_gate(qualified, monkeypatch):
    store, _, _ = qualified
    snapshot = store.snapshot()
    assert len(shortlist(snapshot, dimension=30).cards) == 2
    assert len(shortlist(snapshot, dimension=31).cards) == 0
    with pytest.raises(DomainError) as error:
        snapshot.get("mealpy:fake:Missing", "0" * 64)
    assert error.value.code == "CATALOG_QUALIFICATION_REQUIRED"
    monkeypatch.setattr(storage, "current_library_version", lambda: "0.0.0")
    assert store.snapshot().cards == ()
    assert store.get_snapshot(snapshot.catalog_hash) == snapshot


def test_disabling_blocks_new_jobs_without_rewriting_saved_snapshot(qualified):
    store, _, _ = qualified
    before = store.snapshot()
    card = before.cards[0]
    updated = store.set_availability(
        card.implementation_id, config_id=card.config_id, expected_disabled=False, disabled=True,
    )
    try:
        assert updated["catalog_hash"] != before.catalog_hash
        assert card not in store.snapshot().cards
        assert store.get_snapshot(before.catalog_hash).get(card.implementation_id, card.config_id) == card
        assert store.list_implementations(status="disabled")["total"] == 1
    finally:
        store.set_availability(
            card.implementation_id, config_id=card.config_id, expected_disabled=True, disabled=False,
        )


def _trial(snapshot, revision, card, *, deadline_seconds=10.0):
    seed = derive_seed(1405, revision.problem_math_hash, "pilot", card.seed_identity)
    return catalog_trial_request(
        revision, snapshot=snapshot, implementation_id=card.implementation_id, config_id=card.config_id,
        job_id=str(uuid4()), trial_id=str(uuid4()), attempt_id=str(uuid4()), idempotency_key=str(uuid4()),
        seed=seed, phase="pilot", replicate_index=0, evaluation_quota=500, deadline_seconds=deadline_seconds,
    )


def _execute(trial, revision, tmp_path, **kwargs):
    return run_execution(ExecutionRequest(
        job_id=trial.job_id, execution_id=trial.attempt_id, phase=trial.phase, revision=revision, seed=trial.seed,
        evaluation_quota=trial.evaluation_quota, deadline_seconds=trial.deadline_seconds, trial=trial,
    ), history_path=tmp_path / (trial.attempt_id + ".jsonl"), **kwargs)


def test_num_007_maximize_cancel_timeout_and_guard_import(qualified, tmp_path):
    store, _, _ = qualified
    snapshot = store.snapshot()
    card = snapshot.cards[0]
    sphere = _problem(2, "sphere")
    from p_mc_optimizer.domain.models import ProblemInput
    from p_mc_optimizer.problems.registry import freeze_problem

    data = sphere.spec.model_dump(mode="json", exclude={"source_map"})
    data["objective"]["sense"] = "maximize"
    data["objective"]["source"]["expression"] = "7-(x0**2+x1**2)"
    problem = freeze_problem(ProblemInput.model_validate(data))
    result = _execute(_trial(snapshot, problem, card), problem, tmp_path)
    assert result.termination_reason == "evaluation_budget"
    assert result.started_evaluations == result.successful_evaluations == 500
    assert result.best.score_for_minimization == -result.best.objective_raw

    cancel = _execute(_trial(snapshot, problem, card), problem, tmp_path, cancelled=lambda: True)
    assert cancel.execution_status == "cancelled"
    assert cancel.started_evaluations == 0
    timeout = _execute(_trial(snapshot, problem, card, deadline_seconds=0.001), problem, tmp_path)
    assert timeout.execution_status == "timed_out"
    assert timeout.started_evaluations == 0

    trial = _trial(snapshot, problem, card)
    tampered = trial.model_copy(update={"implementation_id": "mealpy:fake:Missing", "algorithm_id": "mealpy:fake:Missing"})
    result = _execute(tampered, problem, tmp_path)
    assert result.started_evaluations == 0
    assert result.error.error_code == "SOLVER_CONFIGURATION_MISMATCH"


def test_cat_008_cancelled_batch_does_not_consume_fe(tmp_path):
    store = _store(tmp_path)
    config = store.discover()["suggested_configurations"][0]
    qualification_id, _ = store.reserve_batch(
        [config["config_id"]], max_seconds=10, idempotency_key="cancelled-test",
    )
    store.cancel_batch(qualification_id)
    store.run_batch(qualification_id)
    result = store.batch(qualification_id)
    assert result["status"] == "cancelled"
    assert result["started_evaluations"] == result["charged_unknown"] == 0
    assert store.snapshot().cards == ()


def test_cat_006_007_large_shortlist_has_fixed_bound_and_family_order():
    cards = []
    for index in range(10_000):
        config = SolverConfiguration(
            implementation_id=f"mealpy:synthetic.method:C{index:05}",
            library_version="fixture", adapter_version="fixture", parameters=(Parameter(name="epoch", value=1),),
            boundary_repair="fixture",
        )
        report = QualificationReport(
            config_id=config.config_id, status="qualified", pilot_500_compatible=True, checks=(), environment="fixture",
        )
        cards.append(SolverCardV2(
            implementation_id=config.implementation_id, config_id=config.config_id,
            family_id="frequent" if index < 9995 else f"minority{index}",
            library_version="fixture", adapter_version="fixture", seed_identity=config.implementation_id,
            configuration=config, qualified_report=report, pilot_500_compatible=True,
        ))
    cards = tuple(cards)
    snapshot = CatalogSnapshot(catalog_hash=cards_hash(cards), cards=cards)
    tracemalloc.start()
    start = monotonic()
    result = shortlist(snapshot, dimension=10, query="synthetic")
    seconds = monotonic() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(json.dumps({"cards": len(cards), "seconds": seconds, "python_peak_bytes": peak, "system": platform.system(),
                      "machine": platform.machine(), "sqlite": sqlite3.sqlite_version}))
    assert len(result.cards) == 20
    assert len(result.excluded) == 9980
    assert len(set(card.family_id for card in result.cards[:6])) == 6
    assert result == shortlist(snapshot, dimension=10, query="synthetic")
