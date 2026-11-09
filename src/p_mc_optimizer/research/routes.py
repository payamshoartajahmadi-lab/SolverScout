"""Loopback settings endpoints protected by the application's token middleware."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header

from .settings import CredentialTarget, CredentialUpdate, ProfileUpdate, SourcesUpdate


def create_research_router(settings) -> APIRouter:
    router = APIRouter()

    @router.get("/settings/search")
    def get_search():
        return settings.public_profile()

    @router.put("/settings/search")
    def put_search(update: ProfileUpdate):
        return settings.update_profile(update)

    @router.put("/settings/search/credential")
    def put_credential(update: CredentialUpdate):
        return settings.mutate_credential(update)

    @router.delete("/settings/search/credential")
    def delete_credential(target: CredentialTarget):
        return settings.mutate_credential(target)

    @router.post("/settings/search/test")
    def test_search(
        target: CredentialTarget,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    ):
        return settings.test(target, idempotency_key)

    @router.get("/settings/research-sources")
    def get_sources():
        sources = settings.get_sources()
        return {**sources.model_dump(mode="json"), "hash": sources.hash}

    @router.put("/settings/research-sources")
    def put_sources(update: SourcesUpdate):
        sources = settings.update_sources(update)
        return {**sources.model_dump(mode="json"), "hash": sources.hash}

    return router
