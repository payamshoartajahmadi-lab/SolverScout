"""A single, non-streaming Chat Completions attempt through HTTPS."""
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from types import MappingProxyType
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from importlib.resources import files
from typing import Any, Callable

import httpx
from pydantic import ValidationError

from p_mc_optimizer.domain.models import strict_json_loads
from p_mc_optimizer.llm.formulation import RoleOutputError
from p_mc_optimizer.llm.models import (
    ROLE_MODELS,
    SCHEMA_IDS,
    ConnectionTestResponse,
    ProviderSnapshot,
    provider_schema,
)
from p_mc_optimizer.llm.validation import validate_role_output

PROMPT_VERSION = "1.0"
PROMPT_DIR = files("p_mc_optimizer.llm").joinpath("prompts")
_SCHEMA_MODELS = {
    **{SCHEMA_IDS[role]: model for role, model in ROLE_MODELS.items()},
    "connection_test.v1": ConnectionTestResponse,
}
def _schema_model(schema_id):
    if schema_id == "explainer.v2":
        from p_mc_optimizer.llm.explanation_v2 import ExplainerPlanV2

        return ExplainerPlanV2
    if schema_id in _SCHEMA_MODELS:
        return _SCHEMA_MODELS[schema_id]
    from p_mc_optimizer.llm.research_v2 import SCHEMAS

    return SCHEMAS[schema_id][2]


_MAX_HTTP_BODY = 2 * 1024 * 1024
_MAX_REPAIR_OUTPUT = 12000

ERROR_MESSAGES = {
    "PROVIDER_CONFIGURATION_REQUIRED": "تنظیمات یا کلید API این نسخه آماده نیست.",
    "PROVIDER_AUTH_FAILED": "ارائه‌دهنده کلید یا دسترسی مدل را نپذیرفت. کلید و مجوز حساب را بررسی کنید.",
    "UNSUPPORTED_PROVIDER_CONFIGURATION": "مسیر، مدل یا پروتکل ارائه‌دهنده را بررسی کنید.",
    "PROVIDER_STRUCTURED_OUTPUT_UNSUPPORTED": "ارائه‌دهنده حالت خروجی انتخاب‌شده را نپذیرفت.",
    "PROVIDER_RATE_LIMITED": "سقف درخواست یا اعتبار حساب ارائه‌دهنده را بررسی کنید.",
    "PROVIDER_TIMEOUT": "پاسخ ارائه‌دهنده در مهلت تعیین‌شده دریافت نشد.",
    "PROVIDER_UNREACHABLE": "ارتباط با ارائه‌دهنده برقرار نشد. اتصال شبکه را بررسی کنید.",
    "PROVIDER_SERVER_ERROR": "ارائه‌دهنده خطای سرور برگرداند.",
    "LLM_OUTPUT_TRUNCATED": "پاسخ مدل پیش از تکمیل به سقف توکن رسید.",
    "LLM_SCHEMA_INVALID": "خروجی مدل با قالب یا قواعد مورد انتظار سازگار نیست.",
    "UNEXPECTED_PROVIDER_RESPONSE": "شکل پاسخ ارائه‌دهنده با پروتکل مورد انتظار سازگار نیست.",
    "LLM_OUTPUT_REFUSED": "مدل درخواست را نپذیرفت.",
    "LLM_OUTCOME_UNKNOWN": "نتیجهٔ درخواست قبلی نامعلوم است. آن درخواست دوباره ارسال نمی‌شود.",
    "LLM_BUDGET_EXHAUSTED": "سهم تماس این نقش یا اجرا تمام شده است.",
}


def error_detail(
    code: str, *, http_status: int | None = None, field_errors: list[str] | None = None,
    retryable: bool = False, retry_after: str | None = None,
) -> dict:
    return {
        "error_code": code,
        "message_fa": ERROR_MESSAGES[code],
        "field_errors": field_errors or [],
        "retryable": retryable,
        "provider_http_status": http_status,
        "retry_after": retry_after,
    }


_PROMPTS = None
_PROMPT_LOCK = RLock()


def freeze_prompt_bundle():
    global _PROMPTS
    with _PROMPT_LOCK:
        if _PROMPTS is None:
            _PROMPTS = MappingProxyType({path.name[:-4]: path.read_text(encoding="utf-8").strip()
                                        for path in sorted(PROMPT_DIR.iterdir(), key=lambda item: item.name)
                                        if path.name.endswith(".txt")})
        return _PROMPTS


def _prompt(name: str) -> str:
    return freeze_prompt_bundle()[name]


def prompt_bundle_manifest() -> list[dict]:
    return [{"path": name + ".txt", "sha256": sha256(text.encode("utf-8")).hexdigest()}
            for name, text in sorted(freeze_prompt_bundle().items())]


@dataclass(frozen=True)
class PreparedProviderRequest:
    request: dict
    response_schema: dict
    system_message: str
    body_bytes: bytes
    provenance: dict


def prepare_provider_request(snapshot, schema_id, context, *, producer=None) -> PreparedProviderRequest:
    from p_mc_optimizer.runtime_provenance import canonical_bytes, get_runtime

    if context is None:
        data = {}
    elif schema_id == "explainer.v2":
        data = context.model_dump(mode="json", exclude_none=False)
        if data.get("repair") is None:
            data.pop("repair", None)
    else:
        data = context.model_dump(mode="json", exclude_none=True)
    request = build_request(snapshot, schema_id, data)
    response_schema = provider_schema(_schema_model(schema_id))
    body = canonical_bytes(request)
    identity = get_runtime("api")
    provenance = {
        "schema_id": schema_id, "system_message_sha256": sha256(request["messages"][0]["content"].encode("utf-8")).hexdigest(),
        "response_schema_sha256": sha256(canonical_bytes(response_schema)).hexdigest(),
        "request_body_sha256": sha256(body).hexdigest(),
        "prompt_bundle_sha256": sha256(canonical_bytes(prompt_bundle_manifest())).hexdigest(),
        "request_prepared_at_utc": datetime.now(UTC).isoformat(),
        "process_run_id": identity["process_run_id"], "gateway": identity, "producer": producer,
    }
    return PreparedProviderRequest(request, response_schema, request["messages"][0]["content"], body, provenance)


def build_request(snapshot: ProviderSnapshot, schema_id: str, context: dict) -> dict:
    schema = provider_schema(_schema_model(schema_id))
    system = _prompt(schema_id)
    repair = context.get("repair")
    if repair is not None:
        system += "\n\n" + _prompt("repair.v1")
    if snapshot.response_mode != "json_schema":
        system += "\nJSON Schema: " + json.dumps(schema, ensure_ascii=False)
    is_test = schema_id == "connection_test.v1"
    request = {
        "model": snapshot.model,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "Return the connection check as JSON."
                    if is_test else json.dumps(context, ensure_ascii=False, allow_nan=False)
                ),
            },
        ],
        "stream": False,
        "n": 1,
        snapshot.token_limit_field: (
            snapshot.connection_test_max_output_tokens if is_test else snapshot.max_output_tokens
        ),
    }
    if snapshot.response_mode == "json_schema":
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_id.replace(".", "_"),
                "strict": True,
                "schema": schema,
            },
        }
    elif snapshot.response_mode == "json_object":
        request["response_format"] = {"type": "json_object"}
    return request


def _retry_after(value: str | None) -> str | None:
    if value is None or len(value) > 100:
        return None
    try:
        now = datetime.now(UTC)
        deadline = (
            now + timedelta(seconds=int(value)) if value.isascii() and value.isdecimal()
            else parsedate_to_datetime(value)
        )
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return max(now, deadline.astimezone(UTC)).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _safe_usage(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    usage = {
        field: raw[field]
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type(raw.get(field)) is int and raw[field] >= 0
    }
    return usage or None


def _string(value: Any, limit: int = 256) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _http_error(status: int, content: bytes, retry_header: str | None) -> dict:
    if status in (401, 403):
        code = "PROVIDER_AUTH_FAILED"
    elif status == 429:
        code = "PROVIDER_RATE_LIMITED"
    elif status == 408:
        code = "PROVIDER_TIMEOUT"
    elif status >= 500:
        code = "PROVIDER_SERVER_ERROR"
    elif status in (400, 422) and (
        b"response_format" in content.lower() or b"json_schema" in content.lower()
    ):
        code = "PROVIDER_STRUCTURED_OUTPUT_UNSUPPORTED"
    else:
        code = "UNSUPPORTED_PROVIDER_CONFIGURATION"
    return error_detail(
        code,
        http_status=status,
        retryable=status in (408, 429) or status >= 500,
        retry_after=_retry_after(retry_header) if status in (429, 503) else None,
    )


def _outcome(status: str, metadata: dict, *, output=None, error=None, prior=None) -> dict:
    return {
        "status": status,
        "output": output,
        "error": error,
        "metadata": metadata,
        "previous_output": prior,
    }


def call_provider(
    snapshot: ProviderSnapshot,
    key: str,
    schema_id: str,
    context: Any,
    *,
    transport: httpx.BaseTransport | None,
    sanitize: Callable[[Any], Any],
    prepared: PreparedProviderRequest | None = None,
) -> dict:
    """Call the configured destination once and return sanitized, replayable data."""
    start = time.monotonic()
    duration = snapshot.request_timeout_seconds
    metadata = {
        "model_requested": snapshot.model,
        "model_returned": None,
        "request_id": None,
        "finish_reason": None,
        "usage": None,
        "usage_status": "unavailable",
        "cost": None,
        "elapsed_seconds": None,
    }
    prepared = prepared or prepare_provider_request(snapshot, schema_id, context)
    raw_response = bytearray()
    try:
        with httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0, verify=True),
            timeout=httpx.Timeout(duration),
            follow_redirects=False,
            trust_env=False,
        ) as client, client.stream(
            "POST",
            snapshot.base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"},
            content=prepared.body_bytes,
        ) as response:
            metadata["request_id"] = sanitize(_string(response.headers.get("x-request-id")))
            for chunk in response.iter_bytes():
                if time.monotonic() - start >= duration:
                    raise httpx.ReadTimeout("Provider deadline")
                if len(raw_response) + len(chunk) > _MAX_HTTP_BODY:
                    break
                raw_response.extend(chunk)
            else:
                if response.is_error or response.is_redirect:
                    error = _http_error(response.status_code, raw_response, response.headers.get("retry-after"))
                    metadata["elapsed_seconds"] = time.monotonic() - start
                    status = "outcome_unknown" if response.status_code == 408 else "failed"
                    return _outcome(status, metadata, error=error)
                response_bytes = bytes(raw_response)
                return _parse_response(response_bytes, schema_id, context, metadata, start, sanitize)
    except httpx.InvalidURL:
        metadata["elapsed_seconds"] = time.monotonic() - start
        return _outcome("failed", metadata, error=error_detail("UNSUPPORTED_PROVIDER_CONFIGURATION"))
    except httpx.TimeoutException:
        metadata["elapsed_seconds"] = time.monotonic() - start
        return _outcome(
            "outcome_unknown", metadata, error=error_detail("PROVIDER_TIMEOUT", retryable=True)
        )
    except httpx.HTTPError:
        metadata["elapsed_seconds"] = time.monotonic() - start
        return _outcome(
            "outcome_unknown", metadata, error=error_detail("PROVIDER_UNREACHABLE", retryable=True)
        )

    metadata["elapsed_seconds"] = time.monotonic() - start
    return _outcome("failed", metadata, error=error_detail("UNEXPECTED_PROVIDER_RESPONSE"))


def _parse_response(
    response_bytes: bytes, schema_id: str, context: Any, metadata: dict, start: float,
    sanitize: Callable[[Any], Any],
) -> dict:
    metadata["elapsed_seconds"] = time.monotonic() - start
    try:
        body = strict_json_loads(response_bytes)
        if not isinstance(body, dict):
            raise ValueError
    except (ValueError, RecursionError, UnicodeError):
        return _outcome("failed", metadata, error=error_detail("UNEXPECTED_PROVIDER_RESPONSE"))

    metadata["model_returned"] = sanitize(_string(body.get("model")))
    metadata["usage"] = _safe_usage(body.get("usage"))
    if metadata["usage"] is not None:
        metadata["usage_status"] = "reported"
    try:
        choice = body["choices"][0]
        message = choice["message"]
        if not isinstance(message, dict):
            raise TypeError
    except (KeyError, IndexError, TypeError):
        return _outcome("failed", metadata, error=error_detail("UNEXPECTED_PROVIDER_RESPONSE"))

    finish = choice.get("finish_reason")
    metadata["finish_reason"] = sanitize(_string(finish))
    content = message.get("content")
    prior = sanitize(_string(content, _MAX_REPAIR_OUTPUT))
    if finish == "length":
        return _outcome("failed", metadata, error=error_detail("LLM_OUTPUT_TRUNCATED"), prior=prior)
    if message.get("refusal"):
        return _outcome("failed", metadata, error=error_detail("LLM_OUTPUT_REFUSED"))
    if message.get("tool_calls") or message.get("function_call"):
        return _outcome("failed", metadata, error=error_detail("UNEXPECTED_PROVIDER_RESPONSE"))
    try:
        if not isinstance(content, str) or not content.strip():
            raise ValueError
        output = strict_json_loads(content)
        if sanitize(output) != output:
            raise RoleOutputError("response: secret content is prohibited")
        model = _schema_model(schema_id).model_validate(output)
        if schema_id == "explainer.v2":
            from p_mc_optimizer.llm.explanation_v2 import validate_explainer_plan_v2

            validate_explainer_plan_v2(model, context)
        elif schema_id.endswith((".v2", ".v2.1")):
            from p_mc_optimizer.llm.research_v2 import validate_output

            validate_output(schema_id, model, context)
        elif schema_id != "connection_test.v1":
            validate_role_output(schema_id.split(".")[0], model, context)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(map(str, item['loc']))[:200]}: {item['type']}"
            for item in exc.errors(include_input=False, include_url=False, include_context=False)[:30]
        ]
        return _outcome(
            "failed", metadata, error=error_detail("LLM_SCHEMA_INVALID", field_errors=sanitize(errors)), prior=prior
        )
    except RoleOutputError as exc:
        return _outcome(
            "failed", metadata,
            error=error_detail("LLM_SCHEMA_INVALID", field_errors=sanitize(exc.field_errors[:30])), prior=prior,
        )
    except (ValueError, RecursionError, UnicodeError):
        return _outcome(
            "failed", metadata, error=error_detail("LLM_SCHEMA_INVALID"), prior=prior
        )
    return _outcome("succeeded", metadata, output=model.model_dump(mode="json"))
