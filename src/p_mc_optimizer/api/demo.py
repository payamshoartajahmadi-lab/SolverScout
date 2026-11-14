"""The UI and CLI use one durable preparation service."""
from typing import Annotated

from fastapi import APIRouter, Header
from pydantic import Field

from p_mc_optimizer.domain.models import ContractModel


class PrepareDemo(ContractModel):
    enable_official_source: bool = False
    qualification_budget: Annotated[int, Field(ge=1, le=100_000)] = 100_000


def demo_router(service):
    router = APIRouter()

    @router.get("/demo/preparation")
    def status():
        return service.status()

    @router.post("/demo/preparation", status_code=202)
    def start(payload: PrepareDemo, idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]):
        return service.start(**payload.model_dump(), idempotency_key=idempotency_key)

    return router
