"""Authenticated controls for source acquisition and local evidence lookup."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from threading import Event, Thread
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Header, Query
from pydantic import Field, model_validator

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.domain.models import ContractModel
from p_mc_optimizer.research.contracts_v21 import LibrarySourceUpdate, RetrievalSettings, SourceSyncRequest
from p_mc_optimizer.research.settings import CredentialTarget, CredentialUpdate, ProfileUpdate
from p_mc_optimizer.research.storage import canonical

LOGGER = logging.getLogger(__name__)
PROVIDERS = ("tavily",)
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class RetrievalUpdate(RetrievalSettings):
    expected_revision: Annotated[int, Field(ge=1)]
    # Expose a toggle as well as the provider ID for the settings form.
    web_search_enabled: bool | None = None

    @model_validator(mode="after")
    def check_provider(self):
        if self.provider_id not in (None, *PROVIDERS):
            raise ValueError("Unknown search provider")
        return self


class CancelSync(ContractModel):
    expected_version: Annotated[int, Field(ge=1)]


class LibrarySearch(ContractModel):
    query: Annotated[str, Field(min_length=1, max_length=300)]
    source_ids: Annotated[list[str], Field(max_length=300)] | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class StorageLimit(ContractModel):
    limit_bytes: Annotated[int, Field(ge=1, le=10 * 1024**3)]


class LibrarySettings:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _read(conn):
        row = conn.execute("SELECT revision,payload FROM retrieval_settings WHERE singleton=1").fetchone()
        revision = row["revision"] if row else 1
        value = RetrievalSettings.model_validate_json(row["payload"]) if row else RetrievalSettings()
        return revision, value

    def get(self):
        with self.store.connect() as conn:
            revision, value = self._read(conn)
        return {"revision": revision, **value.model_dump(mode="json"),
                "web_search_enabled": value.provider_id is not None}

    def put(self, update):
        with self.store.connect(write=True) as conn:
            revision, _ = self._read(conn)
            if revision != update.expected_revision:
                raise ApiError(409, "STALE_VERSION", "تنظیمات بازیابی تغییر کرده‌اند.")
            value = update.model_dump(exclude={"expected_revision", "web_search_enabled"})
            if update.web_search_enabled is not None:
                value["provider_id"] = "tavily" if update.web_search_enabled else None
            if value["retrieval_mode"] == "web_search" and value["provider_id"] is None:
                raise ApiError(422, "SEARCH_CONFIGURATION_REQUIRED", "موتور جست‌وجو را فعال کنید.")
            conn.execute(
                """INSERT INTO retrieval_settings(singleton,revision,payload) VALUES (1,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,payload=excluded.payload""",
                (revision + 1, canonical(value)),
            )
        return self.get()


def sync_lifespan(synchronizer):
    @asynccontextmanager
    async def lifespan(_app):
        stop = Event()
        worker_id = str(uuid4())

        def work():
            while not stop.is_set():
                try:
                    synchronizer.recover_sync()
                    if synchronizer.run_next(worker_id, stopped=stop.is_set) is not None:
                        continue
                except Exception as exc:
                    LOGGER.error("Library refresh worker failed type=%s", type(exc).__name__)
                stop.wait(0.25)

        thread = Thread(target=work, name="source-library-refresh", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=5)

    return lifespan


def library_router(store, retriever, settings, retrieval, synchronizer):
    router = APIRouter()

    @router.get("/library/sources")
    def sources():
        return store.list_sources()

    @router.put("/library/sources/{source_id}")
    def configure(source_id: str, update: LibrarySourceUpdate):
        return store.configure(source_id, update)

    @router.post("/library/sync-runs", status_code=202)
    def sync(payload: SourceSyncRequest, idempotency_key: IdempotencyKey):
        return synchronizer.start_sync(payload, idempotency_key)

    @router.get("/library/sync-runs")
    def recent_runs():
        return {"runs": synchronizer.list_runs(limit=20)}

    @router.get("/library/sync-runs/{run_id}")
    def sync_status(run_id: str):
        return synchronizer.sync_status(run_id)

    @router.post("/library/sync-runs/{run_id}/cancel")
    def cancel(run_id: str, payload: CancelSync):
        return synchronizer.cancel_sync(run_id, payload.expected_version)

    @router.get("/library/documents")
    def documents(
        source_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return {"documents": retriever.documents(
            settings.get_sources(), source_ids=[source_id] if source_id else None,
            limit=limit, offset=offset,
        )}

    @router.get("/library/documents/{document_id}/versions")
    def versions(document_id: str):
        return {"versions": retriever.versions(document_id, settings.get_sources())}

    @router.post("/library/search")
    def search(payload: LibrarySearch):
        return {"documents": retriever.search(
            payload.query, settings.get_sources(), source_ids=payload.source_ids, limit=payload.limit,
        )}

    @router.post("/library/import-legacy")
    def import_legacy():
        return store.import_legacy()

    @router.get("/library/storage")
    def storage():
        return store.storage_usage()

    @router.put("/library/storage")
    def storage_limit(payload: StorageLimit):
        return store.set_storage_limit(payload.limit_bytes)

    @router.post("/library/storage/cleanup")
    def cleanup():
        return store.purge_unreferenced()

    @router.get("/settings/retrieval")
    def get_retrieval():
        return retrieval.get()

    @router.put("/settings/retrieval")
    def update_retrieval(payload: RetrievalUpdate):
        return retrieval.put(payload)

    @router.get("/settings/search-providers/{provider_id}")
    def provider(provider_id: Literal["tavily"]):
        return settings.public_profile()

    @router.put("/settings/search-providers/{provider_id}")
    def update_provider(provider_id: Literal["tavily"], payload: ProfileUpdate):
        return settings.update_profile(payload)

    @router.put("/settings/search-providers/{provider_id}/credential")
    def set_credential(provider_id: Literal["tavily"], payload: CredentialUpdate):
        return settings.mutate_credential(payload)

    @router.delete("/settings/search-providers/{provider_id}/credential")
    def remove_credential(provider_id: Literal["tavily"], payload: CredentialTarget):
        return settings.mutate_credential(payload)

    @router.post("/settings/search-providers/{provider_id}/test")
    def test_provider(provider_id: Literal["tavily"], payload: CredentialTarget, idempotency_key: IdempotencyKey):
        return settings.test(payload, idempotency_key)

    return router
