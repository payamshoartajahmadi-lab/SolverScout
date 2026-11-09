"""Local-only provider settings and authenticated worker calls."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header

from p_mc_optimizer.llm.gateway import LLMGateway
from p_mc_optimizer.llm.models import CredentialMutationTarget, Invocation, ProfileUpdate, SecretSubmission


def create_llm_router(gateway: LLMGateway) -> APIRouter:
    """The application middleware protects all routes with the instance token."""
    router = APIRouter()

    @router.get("/settings/llm/profile")
    def get_profile(job_id: UUID | None = None):
        return gateway.get_profile(None if job_id is None else str(job_id))

    @router.put("/settings/llm/profile")
    def put_profile(update: ProfileUpdate):
        return gateway.update_profile(update)

    @router.put("/settings/llm/credential")
    def put_credential(secret: SecretSubmission):
        return gateway.mutate_credential(secret)

    @router.delete("/settings/llm/credential")
    def delete_credential(target: CredentialMutationTarget):
        return gateway.mutate_credential(target)

    @router.post("/settings/llm/test")
    def test_profile(
        target: CredentialMutationTarget,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    ):
        return gateway.test_profile(target, idempotency_key)

    @router.get("/settings/llm/models")
    def models():
        return {
            "supported": False,
            "models": [],
            "message_fa": "شناسهٔ دقیق مدل را دستی وارد کنید.",
        }

    @router.post("/internal/llm/invoke")
    def invoke(invocation: Invocation):
        return gateway.invoke(invocation)

    return router
