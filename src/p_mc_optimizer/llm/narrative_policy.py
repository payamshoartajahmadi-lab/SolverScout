"""Server-owned reporting policy, separate from numerical execution policy."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

NARRATIVE_SCHEMAS = {"explainer.v3", "narrative_review.v1"}


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def freeze_narrative_policy(version="1.0", *, max_output_tokens_override=None):
    """Public immutable definition; callers must persist this before dispatch."""
    if version not in {"1.0", "1.1"}:
        raise ValueError("Unknown report generation policy")
    if max_output_tokens_override is not None and (type(max_output_tokens_override) is not int or max_output_tokens_override < 1):
        raise ValueError("Explicit output cap must be a positive integer")
    roles = {
        "explainer.v3": {"prompt_name": "explainer.v3.1" if version == "1.1" else "explainer.v3",
                         "max_output_tokens": 8192 if version == "1.1" else None},
        "narrative_review.v1": {"prompt_name": "narrative_review.v1.1" if version == "1.1" else "narrative_review.v1",
                                "max_output_tokens": 4096 if version == "1.1" else None},
    }
    if max_output_tokens_override is not None:
        for role in roles.values():
            role["max_output_tokens"] = min(role["max_output_tokens"], max_output_tokens_override) if role["max_output_tokens"] is not None else max_output_tokens_override
    value = {"report_generation_policy_version": version, "evidence_policy_version": version,
             "evidence_builder_version": version, "formatter_version": version,
             "renderer_version": "3.0.0", "roles": roles,
             "explicit_max_output_tokens_override": max_output_tokens_override}
    return {**value, "report_generation_policy_hash": _hash(value)}


@dataclass(frozen=True)
class EffectiveNarrativePolicy:
    version: str
    policy_hash: str
    prompt_name: str
    prompt_version: str
    effective_max_output_tokens: int
    snapshot: dict


def resolve_narrative_policy(schema_id, snapshot, *, policy_version="1.0",
                             max_output_tokens_override=None):
    """Resolve actual wire settings without mutating the provider snapshot."""
    frozen = freeze_narrative_policy(policy_version, max_output_tokens_override=max_output_tokens_override)
    role = frozen["roles"].get(schema_id)
    prompt_name = role["prompt_name"] if role else schema_id
    cap = (snapshot.connection_test_max_output_tokens if schema_id == "connection_test.v1"
           else snapshot.max_output_tokens)
    if role and role["max_output_tokens"] is not None:
        cap = role["max_output_tokens"]
    prompt_version = prompt_name.rsplit(".v", 1)[-1]
    if "." not in prompt_version:
        prompt_version += ".0"
    return EffectiveNarrativePolicy(policy_version, frozen["report_generation_policy_hash"],
                                    prompt_name, prompt_version, cap, frozen)
