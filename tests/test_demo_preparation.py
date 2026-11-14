"""Preparation contract/ledger tests; fake checks do not prove solver compatibility."""
from hashlib import sha256
import json

import pytest

from p_mc_optimizer.application.demo import DemoPreparation, TARGETS
from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.catalog.budget_policy import ALLOWLIST
from p_mc_optimizer.persistence import Repository


class CatalogFixture:
    def __init__(self):
        self.rows = {identity: {"implementation_id": identity,
                               "config_id": sha256(identity.encode()).hexdigest(),
                               "status": "discovered", "qualification": None,
                               "budget_policy_status": "not_applicable"} for identity in TARGETS}
        self.batches = {}
        self.fail_reservation = False
        self.crash_after_reservation = False

    def discover(self):
        return self.list_implementations()

    def list_implementations(self, **kwargs):
        return {"items": list(self.rows.values())}

    def reserve_batch(self, configs, *, max_seconds, scope, idempotency_key):
        if self.fail_reservation:
            raise OSError("TEST FIXTURE interruption")
        if idempotency_key in self.batches:
            return idempotency_key, False
        self.batches[idempotency_key] = {
            "status": "running", "scope": scope, "configs": configs, "reports": [],
            "max_evaluations": len(configs) * (3000 if scope == "base" else 31996),
            "started_evaluations": 0, "charged_unknown": 0, "reserved_active": 0,
        }
        if self.crash_after_reservation:
            self.batches[idempotency_key]["status"] = "interrupted"
            self.crash_after_reservation = False
            raise OSError("TEST FIXTURE crash after catalog commit")
        return idempotency_key, True

    def run_batch(self, identity):
        batch = self.batches[identity]
        for row in self.rows.values():
            if row["config_id"] not in batch["configs"]:
                continue
            if batch["scope"] == "base":
                row.update(status="qualified", qualification={"pilot_500_compatible": True})
                if row["implementation_id"] in ALLOWLIST:
                    row["budget_policy_status"] = "NUMERIC_POLICY_QUALIFICATION_REQUIRED"
            else:
                row["budget_policy_status"] = "qualified"
            batch["reports"].append({"status": "qualified"})
        batch.update(status="completed", started_evaluations=batch["max_evaluations"] - 1)

    def batch(self, identity):
        return self.batches[identity]


def fixture(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "fresh data")
    catalog = CatalogFixture()
    service = DemoPreparation(repo, catalog, None, None, None)
    monkeypatch.setattr(service, "_documents", lambda: {
        "enabled": True, "usable_documents": 4, "required_documents_ready": True,
    })
    return repo, catalog, service


def test_missing_certificates_are_run_once_and_reused_across_resume(tmp_path, monkeypatch):
    repo, catalog, service = fixture(tmp_path, monkeypatch)
    result = service.prepare(idempotency_key="first")
    assert result["ready"] and result["status"] == "completed"
    assert result["prepared_count"] == 6
    assert len(catalog.batches) == 2
    assert result["charged_evaluations"] == 81990
    previous = json.loads(service.path.read_text())
    assert service.prepare(idempotency_key="first")["charged_evaluations"] == 81990
    assert service.prepare(idempotency_key="repeat")["ready"]
    assert len(catalog.batches) == 2
    assert json.loads(service.path.read_text())["attempts"] == previous["attempts"]
    with pytest.raises(ApiError, match="DEMO_BUDGET_FROZEN"):
        service.prepare(qualification_budget=99999, idempotency_key="changed-budget")


def test_insufficient_budget_blocks_before_reservation_and_solver(tmp_path, monkeypatch):
    _, catalog, service = fixture(tmp_path, monkeypatch)
    result = service.prepare(qualification_budget=1000)
    assert result["status"] == "blocked" and not result["ready"]
    assert not catalog.batches
    assert result["charged_evaluations"] == result["reserved_evaluations"] == 0
    assert result["errors"][0]["code"] == "DEMO_QUALIFICATION_BUDGET_EXHAUSTED"


def test_completed_same_key_rechecks_invalidated_readiness_without_requalifying(tmp_path, monkeypatch):
    _, catalog, service = fixture(tmp_path, monkeypatch)
    assert service.prepare(idempotency_key="same-request")["ready"]
    monkeypatch.setattr(service, "_documents", lambda: {
        "enabled": True, "usable_documents": 3, "required_documents_ready": False,
    })
    result = service.prepare(idempotency_key="same-request")
    assert not result["ready"] and result["status"] == "blocked"
    assert len(catalog.batches) == 2
    assert result["charged_evaluations"] == 81990


def test_crash_before_catalog_reservation_retains_the_planned_allowance(tmp_path, monkeypatch):
    _, catalog, service = fixture(tmp_path, monkeypatch)
    catalog.fail_reservation = True
    failed = service.prepare(idempotency_key="interrupted")
    assert failed["status"] == "failed"
    assert failed["reserved_evaluations"] == 18000
    catalog.fail_reservation = False
    resumed = service.prepare(idempotency_key="interrupted")
    assert resumed["ready"]
    assert len(resumed["attempts"]) == 2
    assert len(catalog.batches) == 2
    assert resumed["charged_evaluations"] + resumed["reserved_evaluations"] <= 100000


def test_crash_after_catalog_commit_resumes_same_key_without_losing_old_ledger(tmp_path, monkeypatch):
    _, catalog, service = fixture(tmp_path, monkeypatch)
    catalog.crash_after_reservation = True
    assert service.prepare(idempotency_key="same-request")["status"] == "failed"
    resumed = service.prepare(idempotency_key="same-request")
    assert resumed["ready"] and resumed["status"] == "completed"
    assert len(catalog.batches) == len(resumed["attempts"]) == 3
    assert catalog.batches[resumed["attempts"][0]["qualification_id"]]["status"] == "interrupted"
    assert resumed["charged_evaluations"] == 81990
    assert resumed["reserved_evaluations"] == 0
    assert resumed["numeric_runtime_fingerprint_sha256"]


def test_explicit_official_source_activation_preserves_custom_settings(tmp_path):
    from p_mc_optimizer.application.demo import preparation_service, OFFICIAL_SEEDS
    from p_mc_optimizer.research.contracts_v21 import LibrarySourceUpdate
    from p_mc_optimizer.research.settings import SourcesUpdate

    repo = Repository(tmp_path)
    service = preparation_service(repo)
    original = service.settings.get_sources()
    custom = original.sources[1].model_copy(update={"enabled": True})
    service.settings.update_sources(SourcesUpdate(expected_revision=original.revision,
                                                 sources=(original.sources[0], custom, original.sources[2])))
    service._enable_source()
    snapshot = service.settings.get_sources()
    assert snapshot.sources[1] == custom
    assert snapshot.sources[0].enabled
    config = service.library.list_sources()["sources"][0]["config"]
    assert set(config["seed_urls"]) == set(OFFICIAL_SEEDS)
    service._enable_source()
    assert service.settings.get_sources() == snapshot
    assert service.library.list_sources()["sources"][0]["config"] == config
    with repo.connect() as db:
        assert db.execute("SELECT count(*) FROM provider_profiles").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM qualification_runs").fetchone()[0] == 0


def test_source_resume_reuses_saved_queued_run_and_retries_claim_without_new_records(tmp_path, monkeypatch):
    _, _, service = fixture(tmp_path, monkeypatch)
    class Synchronizer:
        def __init__(self):
            self.calls = 0
            self.status = "queued"
        def recover_sync(self):
            return 0
        def sync_status(self, identity):
            assert identity == "saved-run"
            return {"run_id": identity, "status": self.status}
        def start_sync(self, *args):
            raise AssertionError("existing queued run must be reused")
        def run_next(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                self.status = "completed"
    sync = Synchronizer()
    service.synchronizer = sync
    state = service._load()
    state["source_sync_runs"] = ["saved-run"]
    assert service._refresh_sources(state)["status"] == "completed"
    assert sync.calls == 2
    assert state["source_sync_runs"] == ["saved-run"]


def test_source_resume_preserves_interrupted_run_and_creates_one_new_bounded_run(tmp_path, monkeypatch):
    _, _, service = fixture(tmp_path, monkeypatch)
    class Synchronizer:
        def recover_sync(self):
            return 0
        def sync_status(self, identity):
            return {"run_id": identity, "status": "interrupted" if identity == "old-run" else "completed"}
        def start_sync(self, request, key):
            assert request.limits.max_active_seconds == 300
            self.key = key
            return {"run_id": "new-run", "status": "completed"}
    service.synchronizer = Synchronizer()
    state = service._load()
    state["source_sync_runs"] = ["old-run"]
    assert service._refresh_sources(state)["status"] == "completed"
    assert state["source_sync_runs"] == ["old-run", "new-run"]


def test_ui_endpoint_calls_the_shared_preparation_service():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from p_mc_optimizer.api.demo import demo_router

    class Service:
        def status(self):
            return {"status": "not_started", "ready": False}
        def start(self, **kwargs):
            self.received = kwargs
            return {"status": "running", "ready": False}
    service = Service()
    app = FastAPI()
    app.include_router(demo_router(service))
    with TestClient(app) as client:
        assert client.get("/demo/preparation").json()["status"] == "not_started"
        response = client.post("/demo/preparation", json={"enable_official_source": True},
                               headers={"Idempotency-Key": "TEST_FIXTURE_PREPARE"})
        assert response.status_code == 202
        assert service.received == {"enable_official_source": True, "qualification_budget": 100000,
                                    "idempotency_key": "TEST_FIXTURE_PREPARE"}
        assert client.post("/demo/preparation", json={"qualification_budget": 100001},
                           headers={"Idempotency-Key": "invalid"}).status_code == 422


def test_official_document_readiness_rejects_missing_or_corrupt_artifacts(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from p_mc_optimizer.application.demo import OFFICIAL_SEEDS
    from p_mc_optimizer.research.extraction import extraction_profile
    from p_mc_optimizer.research.library_retrieval import LibraryRetriever

    repo, catalog, _ = fixture(tmp_path, monkeypatch)
    class Library:
        def connect(self):
            return nullcontext(None)
        def source_config(self, *args):
            return None
        def previous_document(self, source_id, url):
            return None, {"url": url}
        def verify_artifacts(self, version):
            return None if version["url"] == OFFICIAL_SEEDS[0] else "TEST FIXTURE dedicated sections"
    library = Library()
    settings = SimpleNamespace(get_sources=lambda: SimpleNamespace(sources=[SimpleNamespace(source_id="mealpy_docs", enabled=True)]))
    monkeypatch.setattr(LibraryRetriever, "documents", lambda *args, **kwargs: [
        {"canonical_url": url, "parser_version": extraction_profile(100000), "stale": False} for url in OFFICIAL_SEEDS])
    monkeypatch.setattr("p_mc_optimizer.research.library_retrieval.has_target_evidence", lambda *args: True)
    result = DemoPreparation(repo, catalog, library, settings, None)._documents()
    assert result["usable_documents"] == 3
    assert not result["required_documents_ready"]
