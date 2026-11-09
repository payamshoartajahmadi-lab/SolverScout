"""Local catalog endpoints. Discovery and qualification are separate actions."""

from __future__ import annotations

import logging
from threading import Thread
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query
from pydantic import Field

from p_mc_optimizer.catalog.models import CatalogStatus
from p_mc_optimizer.catalog.store import MAX_QUALIFICATION_CONFIGS, MAX_QUALIFICATION_SECONDS, CatalogStore
from p_mc_optimizer.domain.models import ContractModel, Sha256Hex

LOGGER = logging.getLogger(__name__)


class QualificationRequest(ContractModel):
    schema_version: Literal["2.0"] = "2.0"
    config_ids: Annotated[list[Sha256Hex], Field(min_length=1, max_length=MAX_QUALIFICATION_CONFIGS)]
    max_seconds: Annotated[int, Field(ge=1, le=MAX_QUALIFICATION_SECONDS)] = 120


class AvailabilityRequest(ContractModel):
    schema_version: Literal["2.0"] = "2.0"
    config_id: Sha256Hex
    expected_disabled: bool
    disabled: bool


def catalog_router(store: CatalogStore) -> APIRouter:
    router = APIRouter()

    @router.get("/catalog/implementations")
    def implementations(
        limit: Annotated[int, Query(ge=1, le=250)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        family: Annotated[str | None, Query(max_length=120)] = None,
        status: CatalogStatus | None = None,
        pilot_500_compatible: bool | None = None,
        query: Annotated[str, Query(max_length=240)] = "",
        pilot_500_compatible: bool | None = None,
    ):
        return store.list_implementations(
            limit=limit, offset=offset, family=family, status=status, query=query,
            pilot_500_compatible=pilot_500_compatible,
        )

    @router.put("/catalog/implementations/{implementation_id}/availability")
    def availability(implementation_id: str, payload: AvailabilityRequest):
        return store.set_availability(
            implementation_id, config_id=payload.config_id, expected_disabled=payload.expected_disabled,
            disabled=payload.disabled,
        )

    @router.post("/catalog/discover")
    def discover():
        return store.discover()

    def qualify(qualification_id: str) -> None:
        try:
            store.run_batch(qualification_id)
        except Exception:
            LOGGER.exception("Catalog qualification failed")

    @router.post("/catalog/qualifications", status_code=202)
    def qualifications(payload: QualificationRequest, idempotency_key: Annotated[str, Header(alias="Idempotency-Key")]):
        qualification_id, created = store.reserve_batch(
            payload.config_ids, max_seconds=payload.max_seconds, idempotency_key=idempotency_key,
        )
        if created:
            Thread(target=qualify, args=(qualification_id,), daemon=True, name="catalog-qualification").start()
        return store.batch(qualification_id)

    @router.get("/catalog/qualifications/{qualification_id}")
    def get_qualification(qualification_id: str):
        return store.batch(qualification_id)

    @router.post("/catalog/qualifications/{qualification_id}/cancel")
    def cancel_qualification(qualification_id: str):
        return store.cancel_batch(qualification_id)

    return router
