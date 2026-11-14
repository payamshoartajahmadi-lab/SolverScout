"""Frozen numeric qualification reservations, certificate lookup, and cancellation."""
import json

import pytest

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.catalog import store as module
from p_mc_optimizer.catalog.budget_policy import POLICY_HASH
from p_mc_optimizer.catalog.models import QualificationReport, digest
from p_mc_optimizer.catalog.qualification import NUMERIC_POLICY_CASES, numeric_check_request
from p_mc_optimizer.domain.models import DomainError
from test_catalog_v2 import _GWO, _WOA, _store
from test_budget_policy import budget_qualified_store


@pytest.fixture
def base_fixture(tmp_path, monkeypatch):
    """Controlled base evidence, used only for reservation and snapshot contracts."""
    store = _store(tmp_path)
    candidates = store.discover()["suggested_configurations"]
    chosen = [row for row in candidates if row["implementation_id"] in {_GWO, _WOA}]
    ids = sorted(row["config_id"] for row in chosen)

    def passed(config, **kwargs):
        return QualificationReport(config_id=config.config_id, status="qualified", checks=(),
                                   pilot_500_compatible=True, environment=module.environment_id())

    monkeypatch.setattr(module, "qualify_configuration", passed)
    run, _ = store.reserve_batch(ids, max_seconds=120, idempotency_key="TEST FIXTURE base")
    store.run_batch(run)
    return store, ids, run


def test_numeric_snapshot_missing_certificates_never_changes_catalog(base_fixture):
    store, ids, _ = base_fixture
    before = store.snapshot()
    numeric = store.numeric_execution_snapshot(before)
    assert numeric["policy_hash"] == POLICY_HASH
    assert numeric["certificates"] == {}
    assert {row["base_config_id"] for row in numeric["exclusions"]} == set(ids)
    assert all(row["error_code"] == "NUMERIC_POLICY_QUALIFICATION_REQUIRED" for row in numeric["exclusions"])
    assert store.snapshot() == before
    for config_id, wrapper in numeric["base_qualification_reports"].items():
        assert wrapper["report_hash"] == digest(wrapper["report"])
        assert wrapper["report"]["config_id"] == config_id
    suggestions = store.list_implementations()["suggested_configurations"]
    assert all(row["status"] == "qualified" and row["budget_policy_status"] == "NUMERIC_POLICY_QUALIFICATION_REQUIRED"
               for row in suggestions if row["config_id"] in ids)


def test_numeric_reservation_freezes_matrix_and_replays_old_environment(base_fixture, monkeypatch):
    from p_mc_optimizer.catalog import budget_policy

    store, ids, base_run = base_fixture
    with store._connect() as db:
        base_hash = db.execute("SELECT request_hash FROM qualification_runs WHERE qualification_id=?", (base_run,)).fetchone()[0]
    assert base_hash == digest([ids, 120])
    run, created = store.reserve_batch(ids, max_seconds=300, idempotency_key="TEST FIXTURE policy", scope="budget_horizon.v1")
    assert created
    with store._connect() as db:
        row = dict(db.execute("SELECT * FROM qualification_runs WHERE qualification_id=?", (run,)).fetchone())
    snapshot = json.loads(row["numeric_policy_snapshot"])
    assert snapshot["qualification_profile_id"] == "budget_horizon_checks.v1"
    assert [entry["check_id"] for entry in snapshot["check_matrix"]] == [case[0] for case in NUMERIC_POLICY_CASES]
    assert row["max_evaluations"] == 63992
    assert set(snapshot["base_configurations"]) == set(snapshot["base_qualification_reports"]) == set(ids)
    assert row["request_hash"] == digest({"scope":"budget_horizon.v1", "config_ids":ids, "max_seconds":300,
                                         "policy_hash":snapshot["policy_hash"],
                                         "numeric_runtime_fingerprint_sha256":snapshot["numeric_runtime_fingerprint_sha256"]})

    def forbidden():
        pytest.fail("Replaying a run inspected the current numeric environment")

    monkeypatch.setattr(budget_policy, "numeric_runtime_fingerprint", forbidden)
    monkeypatch.setattr(budget_policy, "numeric_runtime_fingerprint_hash", forbidden)
    assert store.reserve_batch(ids[::-1], max_seconds=300, idempotency_key="TEST FIXTURE policy", scope="budget_horizon.v1") == (run, False)
    with pytest.raises(ApiError) as error:
        store.reserve_batch(ids, max_seconds=299, idempotency_key="TEST FIXTURE policy", scope="budget_horizon.v1")
    assert error.value.error_code == "IDEMPOTENCY_CONFLICT"
    with store._connect() as db:
        assert db.execute("SELECT numeric_policy_snapshot FROM qualification_runs WHERE qualification_id=?", (run,)).fetchone()[0] == row["numeric_policy_snapshot"]


def test_numeric_scope_cancel_before_check_uses_zero_fe(base_fixture):
    store, ids, _ = base_fixture
    run, _ = store.reserve_batch(ids[:1], max_seconds=300, idempotency_key="TEST FIXTURE cancel", scope="budget_horizon.v1")
    store.cancel_batch(run)
    store.run_batch(run)
    first = store.batch(run)
    assert first["scope"] == "budget_horizon.v1" and first["status"] == "cancelled"
    assert first["started_evaluations"] == first["charged_unknown"] == first["reserved_active"] == 0
    assert first["reports"] == []
    store.run_batch(run)
    assert store.batch(run) == first


def test_numeric_scope_issues_only_matrix_bound_authority(base_fixture):
    from p_mc_optimizer.catalog.models import SolverConfiguration

    store, ids, _ = base_fixture
    run, _ = store.reserve_batch(ids[:1], max_seconds=300, idempotency_key="TEST FIXTURE authority", scope="budget_horizon.v1")
    with store._connect() as db:
        snapshot = json.loads(db.execute("SELECT numeric_policy_snapshot FROM qualification_runs WHERE qualification_id=?", (run,)).fetchone()[0])
    config = SolverConfiguration.model_validate_json(json.dumps(snapshot["base_configurations"][ids[0]]))
    entry = snapshot["check_matrix"][0]
    request = numeric_check_request(config, entry, snapshot, run, remaining=60)
    with pytest.raises(DomainError) as error:
        store._reserve_numeric_check(run, config, snapshot, entry, request.model_copy(update={"seed":request.seed+1}))
    assert error.value.code == "NUMERIC_POLICY_AUTHORIZATION_INVALID"
    assert store.batch(run)["reserved_evaluations"] == 0
    authority = store._reserve_numeric_check(run, config, snapshot, entry, request)
    assert authority.request_hash == digest(request.model_dump(mode="json"))
    assert authority.evaluation_quota == 500
    assert store.batch(run)["reserved_active"] == 500
    with store._connect(write=True) as db:
        db.execute("UPDATE qualification_runs SET owner_pid=2147483647 WHERE qualification_id=?", (run,))
    interrupted = store.batch(run)
    assert interrupted["status"] == "interrupted"
    assert interrupted["charged_unknown"] == 500
    assert interrupted["reserved_active"] == 0 and not interrupted["reports"]


def test_numeric_scope_requires_base_qualification(tmp_path):
    store = _store(tmp_path)
    row = next(row for row in store.discover()["suggested_configurations"] if row["implementation_id"] == _GWO)
    with pytest.raises(ApiError) as error:
        store.reserve_batch([row["config_id"]], max_seconds=300, idempotency_key="TEST FIXTURE no base", scope="budget_horizon.v1")
    assert error.value.error_code == "NUMERIC_POLICY_QUALIFICATION_REQUIRED"


def test_real_numeric_qualification_certificates_and_replay_history(budget_qualified_store):
    store = budget_qualified_store
    catalog = store.snapshot()
    numeric = store.numeric_execution_snapshot(catalog)
    assert len(numeric["certificates"]) == 2 and not numeric["exclusions"]
    for config_id, wrapper in numeric["certificates"].items():
        certificate = wrapper["certificate"]
        assert wrapper["certificate_hash"] == digest(certificate)
        assert certificate["base_config_id"] == config_id
        assert certificate["status"] == "qualified" and certificate["pilot_500_compatible"]
        assert len(certificate["checks"]) == 6 and all(row["status"] == "passed" for row in certificate["checks"])
        checks = {row["check"]: row for row in certificate["checks"]}
        assert checks["controlled_failure"]["started_evaluations"] == 1
        assert checks["controlled_failure"]["outcome"] == "OBJECTIVE_EVALUATION_FAILED"
        assert all(checks[name]["started_evaluations"] == quota for name, _, _, _, quota in NUMERIC_POLICY_CASES
                   if name != "controlled_failure")
        assert all(row["history_sha256"] and row["charged_unknown"] == 0 for row in checks.values())
        batch = store.batch(certificate["qualification_id"])
        assert batch["status"] == "completed" and batch["started_evaluations"] == 62994
        assert batch["max_evaluations"] == 63992 and batch["charged_unknown"] == batch["reserved_active"] == 0
        assert batch["unused"] == 998
    assert store.snapshot() == catalog


def test_numeric_environment_mismatch_stops_next_check_without_overwriting_snapshot(base_fixture, monkeypatch):
    from p_mc_optimizer.catalog import budget_policy
    from p_mc_optimizer.catalog.models import SolverConfiguration

    store, ids, _ = base_fixture
    run, _ = store.reserve_batch(ids[:1], max_seconds=300, idempotency_key="TEST FIXTURE drift", scope="budget_horizon.v1")
    with store._connect() as db:
        original = db.execute("SELECT numeric_policy_snapshot FROM qualification_runs WHERE qualification_id=?", (run,)).fetchone()[0]
    snapshot = json.loads(original)
    config = SolverConfiguration.model_validate_json(json.dumps(snapshot["base_configurations"][ids[0]]))
    first = snapshot["check_matrix"][0]
    request = numeric_check_request(config, first, snapshot, run, remaining=60)
    store._reserve_numeric_check(run, config, snapshot, first, request)
    completed = {"check":"sphere_2d", "status":"passed", "started_evaluations":500, "charged_unknown":0,
                 "history_sha256":"a"*64,"outcome":"evaluation_budget","elapsed_seconds":1.0}
    with store._connect(write=True) as db:
        db.execute("UPDATE qualification_checks SET result=? WHERE qualification_id=?", (json.dumps(completed),run))
    second = snapshot["check_matrix"][1]
    next_request = numeric_check_request(config, second, snapshot, run, remaining=59)
    monkeypatch.setattr(budget_policy, "numeric_runtime_fingerprint_hash", lambda: "0"*64)
    with pytest.raises(DomainError) as error:
        store._reserve_numeric_check(run, config, snapshot, second, next_request)
    assert error.value.code == "NUMERIC_POLICY_ENVIRONMENT_MISMATCH"
    with store._connect() as db:
        assert db.execute("SELECT numeric_policy_snapshot FROM qualification_runs WHERE qualification_id=?", (run,)).fetchone()[0] == original
        assert db.execute("SELECT COUNT(*) FROM qualification_checks WHERE qualification_id=?", (run,)).fetchone()[0] == 1
        assert json.loads(db.execute("SELECT result FROM qualification_checks WHERE qualification_id=?", (run,)).fetchone()[0]) == completed


def test_budget_scope_ui_offers_base_qualified_without_policy_certificate(monkeypatch):
    import sys
    from streamlit.testing.v1 import AppTest

    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])

    def render():
        import streamlit as st
        from p_mc_optimizer.ui.catalog import _qualification_actions

        result = {"catalog_hash":"a"*64, "suggested_configurations":[
            {"config_id":"b"*64,"class_name":"OriginalGWO","status":"qualified", "budget_policy_status":"NUMERIC_POLICY_QUALIFICATION_REQUIRED"},
            {"config_id":"c"*64,"class_name":"OriginalWOA","status":"qualified", "budget_policy_status":"qualified"},
        ]}

        def attempt(method,route,data=None,**kwargs):
            if method == "POST":
                st.session_state.sent = data
            return None

        _qualification_actions(attempt,result)

    at = AppTest.from_function(render).run()
    assert not at.exception and len(at.multiselect) == 0
    at.selectbox[0].set_value("budget_horizon.v1").run()
    assert not at.exception and at.multiselect[0].options == ["OriginalGWO"]
    at.multiselect[0].set_value(["b"*64]).run()
    at.button[0].click().run()
    assert at.session_state.sent == {"config_ids":["b"*64],"max_seconds":300,"scope":"budget_horizon.v1"}
