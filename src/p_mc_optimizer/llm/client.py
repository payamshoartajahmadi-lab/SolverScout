"""Workflow client for the loopback gateway. It never sees a provider key."""
from __future__ import annotations

from uuid import uuid4

import httpx

from p_mc_optimizer.llm.models import SCHEMA_IDS


REPAIRABLE_ERROR_CODES = frozenset({"LLM_SCHEMA_INVALID", "LLM_OUTPUT_TRUNCATED"})


def invoke_role(
    api_url: str, internal_token: str, job_id: str, role: str, context: dict,
    *, invocation_id: str | None = None, request_timeout_seconds: int = 60,
    schema_id: str | None = None,
) -> dict:
    """Send one internal invocation, returning the same outcome shape for HTTP errors."""
    body = {
        "job_id": job_id,
        "role": role,
        "schema_id": schema_id or SCHEMA_IDS[role],
        "invocation_id": invocation_id or str(uuid4()),
        "context": context,
    }
    try:
        local_url = httpx.URL(api_url)
        if local_url.scheme != "http" or local_url.host != "127.0.0.1":
            raise ValueError("The gateway must use IPv4 loopback")
        with httpx.Client(
            timeout=httpx.Timeout(request_timeout_seconds + 15),
            follow_redirects=False, trust_env=False,
        ) as client:
            response = client.post(
                api_url.rstrip("/") + "/internal/llm/invoke",
                json=body,
                headers={"X-PMC-Internal-Token": internal_token},
            )
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Invalid local gateway response")
        if response.is_success and "call_id" in result:
            return result
        if not result.get("error_code"):
            raise ValueError("Invalid local gateway response")
        error = result
        status = "failed"
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        error = {
            "error_code": "PROVIDER_UNREACHABLE",
            "message_fa": "درگاه محلی مدل در دسترس نیست. وضعیت برنامه را بررسی کنید.",
            "field_errors": [],
            "retryable": True,
        }
        status = "outcome_unknown"
    return {
        "call_id": None, "status": status, "output": None, "error": error,
        "metadata": None, "previous_output": None,
    }
