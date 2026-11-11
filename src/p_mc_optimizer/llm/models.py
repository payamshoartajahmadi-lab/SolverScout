"""LLM wire contracts. Nullable output fields are still required."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator

from p_mc_optimizer.domain.models import (
    CandidatePlan, ContractModel, FiniteReal, ProblemInput, ProblemProfile, Uuid4String,
)

Role = Literal["formulator", "advisor", "explainer", "research_planner"]
SchemaId = Literal[
    "formulator.v1", "advisor.v1", "explainer.v1", "explainer.v2", "research_planner.v2", "advisor.v2",
    "research_planner.v2.1", "advisor.v2.1",
]
StorageMode = Literal["session", "os_keyring"]
OPENAI_BASE_URL = "https://api.openai.com/v1"


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname
    if host is None:
        raise ValueError("Missing provider host")
    host = host.encode("idna").decode("ascii")
    host = f"[{host}]" if ":" in host else host
    port = parts.port
    return f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"


class VariableDraft(ContractModel):
    name: str | None
    label_fa: str | None
    type: Literal["real", "integer", "binary", "other"] | None
    lower: FiniteReal | None
    upper: FiniteReal | None
    unit: str | None


class ObjectiveDraft(ContractModel):
    sense: Literal["minimize", "maximize"] | None
    source_kind: Literal["expression", "registered_function"] | None
    expression: str | None
    function_id: str | None
    function_version: str | None
    unit: str | None

    @model_validator(mode="after")
    def source_fields_are_consistent(self) -> Self:
        if self.source_kind == "expression" and (
            self.function_id is not None or self.function_version is not None
        ):
            raise ValueError("An expression has no registered function fields")
        if self.source_kind == "registered_function" and self.expression is not None:
            raise ValueError("A registered function has no expression field")
        return self


class ParameterDraft(ContractModel):
    name: str
    value: FiniteReal | None


class ConstraintDraft(ContractModel):
    source_text: str
    expression: str | None


class PartialProblemSpec(ContractModel):
    variables: list[VariableDraft] | None
    objective: ObjectiveDraft | None
    parameters: list[ParameterDraft] | None
    constraints: list[ConstraintDraft] | None
    deterministic: bool | None


class FieldEvidence(ContractModel):
    field_path: str
    source_ref: str
    quote: str


class ClarificationQuestion(ContractModel):
    field_paths: list[str]
    reason: Literal["missing", "ambiguous", "contradictory"]
    question_fa: str


class UnsupportedFeature(ContractModel):
    kind: str
    description_fa: str
    evidence_refs: list[str]


class Normalization(ContractModel):
    field_path: str
    source_text: str
    normalized_text: str
    reason_fa: str


class FormulatorResponse(ContractModel):
    extracted_fields: PartialProblemSpec
    source_map: list[FieldEvidence]
    clarification_questions: list[ClarificationQuestion]
    unsupported_features: list[UnsupportedFeature]
    normalizations: list[Normalization]


class ExplainerResponse(ContractModel):
    summary_fa: str
    selection_explanation_fa: str | None
    limitations_fa: list[str]
    evidence_refs: list[str]


class ConnectionTestResponse(ContractModel):
    ok: bool

    @field_validator("ok")
    @classmethod
    def expected_value(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Expected a successful Boolean result")
        return value


ROLE_MODELS = {
    "formulator": FormulatorResponse,
    "advisor": CandidatePlan,
    "explainer": ExplainerResponse,
}
SCHEMA_IDS = {role: f"{role}.v1" for role in ROLE_MODELS}


def provider_schema(model: type[ContractModel]) -> dict:
    """Project the shared DTO schema onto the provider's strict subset."""
    unsupported = {
        "title", "default", "format", "minLength", "maxLength", "pattern",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minItems", "maxItems", "uniqueItems",
    }

    def convert(node: dict) -> dict:
        result = {key: value for key, value in node.items() if key not in unsupported}
        for key in ("properties", "$defs"):
            if key in result:
                result[key] = {name: convert(value) for name, value in result[key].items()}
        if "items" in result:
            result["items"] = convert(result["items"])
        for key in ("anyOf", "oneOf"):
            if key in result:
                result[key] = [convert(item) for item in result[key]]
        if "const" in result:
            result["enum"] = [result.pop("const")]
        if result.get("type") == "object":
            properties = result.get("properties", {})
            if set(result.get("required", [])) != set(properties):
                raise ValueError("All role output fields must be required")
            if result.get("additionalProperties") is not False:
                raise ValueError("Role output may not contain an open object")
        return result

    return convert(model.model_json_schema())


class ProviderSettings(ContractModel):
    preset: Literal["openai", "custom_openai_compatible"]
    base_url: Annotated[str, Field(min_length=1, max_length=2048)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    protocol: Literal["chat_completions_v1"] = "chat_completions_v1"
    response_mode: Literal["json_schema", "json_object", "text_json"]
    token_limit_field: Literal["max_completion_tokens", "max_tokens"]
    max_output_tokens: Annotated[int, Field(ge=256, le=16384)] = 4096
    connection_test_max_output_tokens: Annotated[int, Field(ge=128, le=4096)] = 512
    request_timeout_seconds: Annotated[int, Field(ge=5, le=300)] = 60

    @field_validator("model")
    @classmethod
    def model_is_exact(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or any(c.isspace() for c in value):
            raise ValueError("Enter the exact model identifier")
        return value

    @field_validator("base_url")
    @classmethod
    def https_base(cls, value: str) -> str:
        if any(ord(c) < 33 or c in "\\?#" for c in value):
            raise ValueError("Base URL cannot contain whitespace, query or fragment")
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username is not None:
            raise ValueError("Base URL must be HTTPS without user information")
        if "%" in parts.netloc:
            raise ValueError("Base URL host must be explicit")
        origin_of(value)
        base = value.rstrip("/")
        if base.lower().endswith("/chat/completions"):
            raise ValueError("Enter the base URL without /chat/completions")
        return base

    @model_validator(mode="after")
    def preset_origin_is_exact(self) -> Self:
        if self.preset == "openai" and self.base_url != OPENAI_BASE_URL:
            raise ValueError("Use the custom preset for a non-OpenAI base URL")
        return self


class ProfileUpdate(ProviderSettings):
    expected_revision: Annotated[int, Field(gt=0)] | None


class ProviderSnapshot(ProviderSettings):
    profile_id: str
    revision: int
    credential_ref: str
    provider_snapshot_hash: str


class CredentialMutationTarget(ContractModel):
    profile_id: UUID
    profile_revision: Annotated[int, Field(gt=0)]
    expected_credential_generation: Annotated[int, Field(ge=0)]
    target_job_id: UUID | None

    @field_validator("profile_id", "target_job_id", mode="before")
    @classmethod
    def parse_uuid(cls, value: str | None) -> UUID | None:
        return UUID(value) if isinstance(value, str) else value


class SecretSubmission(CredentialMutationTarget):
    api_key: Annotated[SecretStr, Field(json_schema_extra={"writeOnly": True})]
    storage_mode: StorageMode

    @field_validator("api_key", mode="before")
    @classmethod
    def validate_secret(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value or any(
            ord(c) < 33 or ord(c) > 126 for c in value
        ):
            raise ValueError("API key must contain printable non-space ASCII characters")
        return value


class CredentialStatus(ContractModel):
    profile_id: str
    profile_revision: int
    credential_generation: int
    configured: bool
    available: bool
    storage_mode: StorageMode | None
    error: str | None


class Repair(ContractModel):
    previous_output: Annotated[str, Field(max_length=12000)] | None
    field_errors: list[Annotated[str, Field(max_length=320)]]


class Source(ContractModel):
    source_ref: Annotated[str, Field(min_length=1, max_length=160)]
    text: Annotated[str, Field(min_length=1, max_length=16000)]
    question_fa: Annotated[str, Field(max_length=16000)] | None = None


class FormulatorContext(ContractModel):
    sources: Annotated[list[Source], Field(min_length=1, max_length=40)]
    repair: Repair | None = None

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        ids = [source.source_ref for source in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate source identifier")
        return self


class AdvisorContext(ContractModel):
    problem: ProblemInput
    profile: ProblemProfile
    solver_cards: list[dict[str, Any]]
    candidate_count: Literal[1, 3]
    evidence_refs: list[str]
    repair: Repair | None = None


class ExplainerPilotMetric(ContractModel):
    algorithm_id: str
    eligible: bool
    trial_ids: list[Uuid4String]
    median_score: FiniteReal | None
    normalized_iqr: Annotated[FiniteReal, Field(ge=0)] | None
    reason: str | None

    @field_validator("algorithm_id")
    @classmethod
    def algorithm_id_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("An algorithm identifier is required")
        return value

    @model_validator(mode="after")
    def eligible_scores_are_present(self) -> Self:
        if self.eligible and (self.median_score is None or self.normalized_iqr is None):
            raise ValueError("Eligible metrics require both a median and an IQR")
        return self


class ExplainerDecisionSummary(ContractModel):
    schema_version: Literal["1.0"]
    mode: Literal["empirical_pilots", "policy_assignment"]
    decision_source: str
    requested_selection_policy: str
    effective_selection_policy: str
    objective_sense: Literal["minimize", "maximize"]
    score_semantics: Literal["raw_objective", "negated_objective"]
    score_order: Literal["lower_is_better"]
    pilot_repeats: Annotated[int, Field(ge=0)]
    pilot_evaluations: Annotated[int, Field(ge=0)]
    scale: Annotated[FiniteReal, Field(gt=0)] | None
    tie_break_order: list[str]
    metrics: list[ExplainerPilotMetric]
    eligible_ranking: list[str]
    decisive_criterion: Literal["median_score", "normalized_iqr", "algorithm_id"] | None

    @model_validator(mode="after")
    def ranking_matches_eligible_metrics(self) -> Self:
        identifiers = {metric.algorithm_id for metric in self.metrics}
        if len(identifiers) != len(self.metrics):
            raise ValueError("Duplicate algorithm identifiers in metrics")
        eligible = {metric.algorithm_id for metric in self.metrics if metric.eligible}
        if set(self.eligible_ranking) != eligible or len(self.eligible_ranking) != len(eligible):
            raise ValueError("The ranking must contain each eligible algorithm exactly once")
        return self


class ExplainerContext(ContractModel):
    report: dict[str, Any]
    evidence_refs: list[str]
    selection_made: bool
    repair: Repair | None = None


ROLE_CONTEXTS = {
    "formulator": FormulatorContext,
    "advisor": AdvisorContext,
    "explainer": ExplainerContext,
}


class Invocation(ContractModel):
    job_id: UUID
    role: Role
    schema_id: SchemaId
    invocation_id: UUID
    context: dict[str, Any]

    @field_validator("job_id", "invocation_id", mode="before")
    @classmethod
    def parse_uuid(cls, value: str) -> UUID:
        return UUID(value) if isinstance(value, str) else value
