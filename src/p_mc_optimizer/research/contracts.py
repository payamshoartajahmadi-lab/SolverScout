"""Strict, immutable v2 contracts for web research and catalog selection."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Annotated, Literal, Mapping, Set

from pydantic import AfterValidator, BeforeValidator, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from p_mc_optimizer.domain.models import (
    NUMERIC_ATOL,
    NUMERIC_RTOL,
    PILOT_EVALUATIONS,
    PILOT_REPEATS,
    PREFLIGHT_EVALUATIONS,
    PROFILE_EVALUATIONS,
    VERIFICATION_EVALUATIONS,
    VERIFICATION_TIME_RESERVE_SECONDS,
    ContractModel,
    DomainError,
    FiniteReal,
    JobInput,
    ProfileReference,
    StructuredProblemInput,
    strict_json_loads,
)
from p_mc_optimizer.problems.registry import content_hash
from p_mc_optimizer.research.source_policy import SourcePolicy, canonical_path, normalize_hostname

TAVILY_ENDPOINT = "https://api.tavily.com/search"
_SITE_OPERATOR = re.compile(r"(?i)(?<![\w])site\s*:\s*([^\s]*)")


def _as_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


type FrozenArray[T] = Annotated[tuple[T, ...], BeforeValidator(_as_tuple)]
Version = Literal["2.0"]
ShortId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Id = Annotated[str, Field(min_length=1, max_length=256)]
ImplementationId = Annotated[
    str, Field(max_length=256, pattern=r"^[a-z][a-z0-9_]*:[A-Za-z0-9_.]+:[A-Za-z_][A-Za-z0-9_]*$")
]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
URL = Annotated[str, Field(min_length=1, max_length=4096)]
CacheAgeHours = Annotated[int, Field(ge=1, le=720)]
NonnegativeSeconds = Annotated[FiniteReal, Field(ge=0)]
ContentLevel = Literal["snippet", "abstract", "documentation", "full_text"]
ResearchMode = Literal["live", "cache_only"]
ResearchFailurePolicy = Literal["stop", "continue_with_catalog"]
PolicyIdV2 = Literal["web_research_then_pilot", "catalog_llm_then_pilot", "catalog_fixed"]
ResearchStatus = Literal[
    "not_requested", "pending", "running", "completed", "partial",
    "insufficient_evidence", "failed", "cancelled"
]


def _utc_timestamp(value: object) -> object:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("A timezone-aware UTC timestamp is required.")
    return value


UtcTimestamp = Annotated[datetime, BeforeValidator(_utc_timestamp)]


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Nonblank text is required.")
    return value


NonblankText = Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(_nonblank)]


class VersionedContract(ContractModel):
    schema_version: Version = "2.0"

    @classmethod
    def from_json(cls, value: str | bytes, *, limit: int = 1024 * 1024):
        return cls.model_validate(strict_json_loads(value, limit=limit))


class SourceRule(VersionedContract):
    source_id: ShortId
    label: Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_nonblank)]
    hostname: Annotated[str, Field(min_length=1, max_length=254)]
    include_subdomains: bool = False
    path_prefixes: Annotated[FrozenArray[str], Field(min_length=1, max_length=20)]
    source_kind: Literal["official_docs", "paper_repository", "code_repository", "benchmark_report"]
    enabled: bool

    @field_validator("hostname")
    @classmethod
    def check_hostname(cls, value: str) -> str:
        return normalize_hostname(value)

    @field_validator("path_prefixes")
    @classmethod
    def check_prefixes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(value) > 2048 for value in values):
            raise ValueError("Source path is too long.")
        paths = tuple(canonical_path(path).rstrip("/") or "/" for path in values)
        if len(set(paths)) != len(paths):
            raise ValueError("Source path prefixes must be distinct.")
        return paths

    def matches_hostname(self, hostname: str) -> bool:
        return hostname == self.hostname or (
            self.include_subdomains and hostname.endswith("." + self.hostname)
        )


class SourceSnapshot(VersionedContract):
    revision: Annotated[int, Field(ge=1)]
    sources: FrozenArray[SourceRule]

    @model_validator(mode="after")
    def check_unique_sources(self):
        if len({rule.source_id for rule in self.sources}) != len(self.sources):
            raise ValueError("Source IDs must be distinct.")
        return self

    @property
    def active_sources(self) -> tuple[SourceRule, ...]:
        return tuple(rule for rule in self.sources if rule.enabled)

    @property
    def hash(self) -> str:
        return content_hash([rule.model_dump(mode="json") for rule in self.sources])


class ResearchLimits(ContractModel):
    max_search_attempts: Annotated[int, Field(ge=1, le=3)] = 3
    max_queries: Annotated[int, Field(ge=1, le=3)] = 3
    max_results_per_query: Annotated[int, Field(ge=1, le=5)] = 5
    max_documents: Annotated[int, Field(ge=1, le=6)] = 6
    max_redirects: Annotated[int, Field(ge=0, le=3)] = 3
    max_http_requests: Annotated[int, Field(ge=1, le=27)] = 27
    request_timeout_seconds: Annotated[FiniteReal, Field(gt=0, le=30)] = 20.0
    active_timeout_seconds: Annotated[FiniteReal, Field(gt=0, le=180)] = 120.0
    max_document_bytes: Annotated[int, Field(ge=1, le=10 * 1024 * 1024)] = 5 * 1024 * 1024
    max_total_bytes: Annotated[int, Field(ge=1, le=30 * 1024 * 1024)] = 20 * 1024 * 1024
    max_text_chars: Annotated[int, Field(ge=1, le=100_000)] = 60_000
    max_passage_chars: Annotated[int, Field(ge=1, le=24_000)] = 18_000
    max_solver_cards: Annotated[int, Field(ge=1, le=20)] = 20


class SearchProfile(VersionedContract):
    profile_id: Id
    revision: Annotated[int, Field(ge=1)]
    provider_id: Literal["tavily"] = "tavily"
    endpoint: Literal["https://api.tavily.com/search"] = TAVILY_ENDPOINT
    limits: ResearchLimits = ResearchLimits()
    credential_ref: Id | None = None


class ResearchSnapshot(VersionedContract):
    policy_version: Version = "2.0"
    source_snapshot: SourceSnapshot
    search_profile: SearchProfile | None
    research_mode: ResearchMode
    catalog_hash: Sha256
    max_cache_age_hours: CacheAgeHours = 24
    on_research_failure: ResearchFailurePolicy = "stop"
    planner_prompt_version: Literal["research_planner.v2"] = "research_planner.v2"
    advisor_prompt_version: Literal["advisor.v2"] = "advisor.v2"

    @property
    def hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class SearchProblemProfile(VersionedContract):
    """Lossy description. There is no field for expressions, names or samples."""

    n_variables: Annotated[int, Field(ge=2, le=30)]
    variable_type: Literal["real"] = "real"
    objective_sense: Literal["minimize", "maximize"]
    bounded: Literal[True] = True
    derivative_requirement: Literal["not_required", "unknown"] = "not_required"
    deterministic: Literal[True] = True
    tags: FrozenArray[Literal["sample_variation_low", "sample_variation_high"]] = ()


def _normalize_query(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


class ResearchQuery(ContractModel):
    query_id: ShortId
    text: Annotated[str, Field(min_length=1, max_length=300)]
    source_ids: Annotated[FrozenArray[ShortId], Field(min_length=1)]
    purpose: Literal["method_discovery", "implementation_evidence"]

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = _normalize_query(value)
        if not 1 <= len(normalized) <= 300 or any(
            unicodedata.category(character).startswith("C") for character in normalized
        ):
            raise ValueError("Query must have 1 to 300 normalized, printable characters.")
        return normalized

    @field_validator("source_ids")
    @classmethod
    def check_distinct_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Query source IDs must be distinct.")
        return value


class ResearchQueryPlan(VersionedContract):
    schema_version: Version
    queries: Annotated[FrozenArray[ResearchQuery], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def check_distinct_query_ids(self):
        if len({query.query_id for query in self.queries}) != len(self.queries):
            raise ValueError("Query IDs must be distinct.")
        return self

    def normalized(
        self, source_snapshot: SourceSnapshot, limits: ResearchLimits | None = None
    ) -> ResearchQueryPlan:
        active = {rule.source_id: rule for rule in source_snapshot.active_sources}
        remaining = []
        seen = set()
        for query in self.queries:
            if set(query.source_ids) - active.keys():
                raise DomainError("SOURCE_NOT_ALLOWED", "Query names an inactive or unknown source.")
            rules = tuple(active[source_id] for source_id in query.source_ids)
            for match in _SITE_OPERATOR.finditer(query.text):
                authority, sep, path = match.group(1).partition("/")
                hostname = normalize_hostname(authority)
                if sep:
                    SourcePolicy(source_snapshot).match(
                        f"https://{hostname}/{path}", query.source_ids
                    )
                if not any(rule.matches_hostname(hostname) for rule in rules):
                    raise DomainError("SOURCE_NOT_ALLOWED", "Query site operator is outside its sources.")
            key = (query.text, tuple(sorted(query.source_ids)), query.purpose)
            if key not in seen:
                seen.add(key)
                remaining.append(query)
        if limits is not None and len(remaining) > limits.max_queries:
            raise DomainError("RESEARCH_BUDGET_EXHAUSTED", "Query plan exceeds the configured query limit.")
        return ResearchQueryPlan(schema_version="2.0", queries=tuple(remaining))


class SearchRequest(VersionedContract):
    invocation_id: Id
    query_id: ShortId
    text: Annotated[str, Field(min_length=1, max_length=300)]
    source_ids: Annotated[FrozenArray[ShortId], Field(min_length=1)]
    snapshot_hash: Sha256


class SearchHit(VersionedContract):
    query_id: ShortId
    url: URL
    title: Annotated[str, Field(max_length=500)] = ""
    snippet: Annotated[str, Field(max_length=4000)] = ""
    provider_rank: Annotated[int, Field(ge=1)]
    published_at: UtcTimestamp | None = None
    updated_at: UtcTimestamp | None = None
    source_id: ShortId | None = None
    canonical_url: URL | None = None
    rejection_code: ShortId | None = None


class SourceDocument(VersionedContract):
    document_id: Id
    source_id: ShortId
    requested_url: URL
    final_url: URL | None = None
    redirect_chain: FrozenArray[URL] = ()
    fetched_at: UtcTimestamp | None = None
    validated_at: UtcTimestamp | None = None
    published_at: UtcTimestamp | None = None
    updated_at: UtcTimestamp | None = None
    content_type: str | None = None
    content_level: ContentLevel | None = None
    parser_version: Id
    bytes_sha256: Sha256 | None = None
    text_sha256: Sha256 | None = None
    fetch_status: Literal["read", "failed", "revalidated", "cached"]
    reason_code: ShortId | None = None
    truncated: bool = False
    etag: str | None = None
    last_modified: str | None = None
    cache_age_hours: Annotated[FiniteReal, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def check_provenance(self):
        if self.fetch_status == "failed":
            if self.reason_code is None:
                raise ValueError("Failed documents require a reason code.")
            return self
        required = (
            self.final_url, self.content_type, self.content_level, self.fetched_at,
            self.validated_at, self.bytes_sha256, self.text_sha256,
        )
        if any(value is None for value in required):
            raise ValueError("Readable documents require text and network provenance.")
        if self.fetched_at > self.validated_at:
            raise ValueError("The validation timestamp cannot predate the download.")
        if self.truncated and self.content_level == "full_text":
            raise ValueError("An incomplete extraction cannot be labelled full text.")
        return self


class EvidencePassage(VersionedContract):
    evidence_id: Id
    document_id: Id
    text: Annotated[str, Field(min_length=1, max_length=1500)]
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    page: Annotated[int, Field(ge=1)] | None = None
    section: str | None = None
    keyword_matches: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def check_offsets(self):
        if self.end - self.start != len(self.text):
            raise ValueError("Passage length must equal its recorded offset span.")
        return self

    def verify(self, document: SourceDocument, extracted_text: str) -> None:
        if (
            self.document_id != document.document_id
            or document.fetch_status == "failed"
            or document.content_level == "snippet"
            or self.text != extracted_text[self.start:self.end]
            or hashlib.sha256(extracted_text.encode("utf-8")).hexdigest() != document.text_sha256
        ):
            raise DomainError("EVIDENCE_REFERENCE_INVALID", "Passage does not resolve to the recorded text.")


class ResearchOutcome(VersionedContract):
    status: ResearchStatus
    query_plan: ResearchQueryPlan | None = None
    hits: FrozenArray[SearchHit] = ()
    search_attempts: Annotated[int, Field(ge=0, le=3)] = 0
    http_requests: Annotated[int, Field(ge=0, le=27)] = 0
    active_seconds: NonnegativeSeconds = 0.0
    expanded_bytes: Annotated[int, Field(ge=0)] = 0
    documents: FrozenArray[SourceDocument] = ()
    evidence: FrozenArray[EvidencePassage] = ()
    cost: Annotated[FiniteReal, Field(ge=0)] | None = None
    reason_codes: FrozenArray[ShortId] = ()
    cache_only: bool = False
    retry_after: str | None = None

    @model_validator(mode="after")
    def check_evidence(self):
        docs = {row.document_id: row for row in self.documents}
        if len(docs) != len(self.documents):
            raise ValueError("Document IDs must be distinct.")
        if len({row.evidence_id for row in self.evidence}) != len(self.evidence):
            raise ValueError("Evidence IDs must be distinct.")
        for row in self.evidence:
            doc = docs.get(row.document_id)
            if doc is None or doc.fetch_status == "failed" or doc.content_level == "snippet":
                raise ValueError("Passage requires a readable, eligible document in the outcome.")
        if self.cache_only and (self.search_attempts or self.http_requests):
            raise ValueError("Cache-only research cannot contain network calls.")
        return self


class AdvisorCandidateV2(ContractModel):
    implementation_id: ImplementationId
    config_id: Id
    rationale_fa: NonblankText
    card_ref: Id
    evidence_refs: FrozenArray[Id]
    evidence_scope: Literal["method_level", "family_level"] | None

    @model_validator(mode="after")
    def check_evidence_scope(self):
        if bool(self.evidence_refs) != (self.evidence_scope is not None):
            raise ValueError("An evidence scope must accompany the references.")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("Evidence references must be distinct.")
        return self


class EvidenceClaim(ContractModel):
    text: NonblankText
    status: Literal["supported", "uncertain", "conflicting"]
    evidence_refs: Annotated[FrozenArray[Id], Field(min_length=1)]


class ImplementationMatch(ContractModel):
    implementation_id: ImplementationId
    config_id: Id
    card_ref: Id


class ResearchMethodCandidate(ContractModel):
    name: NonblankText
    variant: str | None
    claims: FrozenArray[EvidenceClaim]
    uncertainty: str | None
    implementation_match: ImplementationMatch | None
    mapping_reason: NonblankText


class AdvisorPlanV2(VersionedContract):
    schema_version: Version
    status: Literal["ready", "insufficient_evidence"]
    candidates: Annotated[FrozenArray[AdvisorCandidateV2], Field(max_length=3)]
    research_findings: Annotated[FrozenArray[ResearchMethodCandidate], Field(max_length=10)]
    reason_codes: FrozenArray[ShortId]
    uncertainty_notes: str | None

    @model_validator(mode="after")
    def check_status(self):
        if self.status == "ready":
            if len({row.implementation_id for row in self.candidates}) != 3:
                raise ValueError("A ready plan needs three distinct implementations.")
        elif self.candidates or not self.reason_codes:
            raise ValueError("An insufficient plan requires reasons and no executable candidates.")
        return self

    def validate_references(
        self,
        cards: Mapping[str, tuple[str, str]],
        evidence_ids: Set[str],
        *,
        require_web: bool,
    ) -> None:
        for row in self.candidates:
            if cards.get(row.card_ref) != (row.implementation_id, row.config_id):
                raise DomainError("IMPLEMENTATION_MAPPING_INVALID", "Candidate is outside the catalog snapshot.")
            if (require_web and not row.evidence_refs) or set(row.evidence_refs) - evidence_ids:
                raise DomainError("EVIDENCE_REFERENCE_INVALID", "Candidate does not cite eligible Job evidence.")
        for finding in self.research_findings:
            match = finding.implementation_match
            if match is not None and cards.get(match.card_ref) != (match.implementation_id, match.config_id):
                raise DomainError("IMPLEMENTATION_MAPPING_INVALID", "Research match is outside the catalog snapshot.")
            for claim in finding.claims:
                if set(claim.evidence_refs) - evidence_ids:
                    raise DomainError("EVIDENCE_REFERENCE_INVALID", "Claim cites evidence outside the Job.")


class ExecutionPolicyInputV2(ContractModel):
    policy_version: Version
    portfolio_id: Literal["continuous_catalog_v2"]
    selection_policy: PolicyIdV2
    max_objective_evaluations: Annotated[int, Field(ge=1002, le=1_000_000)]
    max_compute_seconds: Annotated[int, Field(ge=11, le=3600)]
    master_seed: Annotated[int, Field(ge=0, lt=2**32)]
    explain_via_llm: bool
    fixed_implementation_id: ImplementationId | None = None
    fixed_config_id: Id | None = None
    research_mode: ResearchMode = "live"
    on_research_failure: ResearchFailurePolicy = "stop"
    max_cache_age_hours: CacheAgeHours = 24

    @model_validator(mode="after")
    def check_policy(self):
        fixed = self.selection_policy == "catalog_fixed"
        if (self.fixed_implementation_id is not None) != fixed or (self.fixed_config_id is not None) != fixed:
            raise ValueError("A fixed implementation and config are required only for catalog_fixed.")
        if fixed and self.explain_via_llm:
            raise ValueError("Catalog fixed does not call the language model.")
        if self.selection_policy != "web_research_then_pilot" and (
            self.research_mode != "live" or self.on_research_failure != "stop" or self.max_cache_age_hours != 24
        ):
            raise ValueError("Research settings require a web research policy.")
        if self.main_evaluations < 500:
            raise ValueError("At least 500 main evaluations are required.")
        return self

    @property
    def candidate_count(self) -> int:
        return 0 if self.selection_policy == "catalog_fixed" else 3

    @property
    def main_evaluations(self) -> int:
        return (
            self.max_objective_evaluations - PREFLIGHT_EVALUATIONS - PROFILE_EVALUATIONS
            - self.candidate_count * PILOT_REPEATS * PILOT_EVALUATIONS - VERIFICATION_EVALUATIONS
        )


_LLM_CALL_LIMITS = {"catalog_fixed": 0, "catalog_llm_then_pilot": 6, "web_research_then_pilot": 8}


class ExecutionPolicyV2(ExecutionPolicyInputV2):
    preflight_evaluations: Literal[8] = PREFLIGHT_EVALUATIONS
    profile_evaluations: Literal[492] = PROFILE_EVALUATIONS
    pilot_repeats: Literal[3] = PILOT_REPEATS
    pilot_evaluations: Literal[500] = PILOT_EVALUATIONS
    verification_evaluations: Literal[2] = VERIFICATION_EVALUATIONS
    verification_time_reserve_seconds: Literal[10] = VERIFICATION_TIME_RESERVE_SECONDS
    max_llm_calls: Literal[0, 6, 8]
    max_llm_calls_per_role: Literal[2] = 2
    max_trial_attempts: Literal[1] = 1
    numeric_atol: Literal[1e-12] = NUMERIC_ATOL
    numeric_rtol: Literal[1e-10] = NUMERIC_RTOL
    use_history: Literal[False] = False
    pilot_candidate_count: Literal[0, 3]

    @classmethod
    def from_input(cls, policy: ExecutionPolicyInputV2) -> ExecutionPolicyV2:
        return cls.model_validate(policy.model_dump() | {
            "pilot_candidate_count": policy.candidate_count,
            "max_llm_calls": _LLM_CALL_LIMITS[policy.selection_policy],
        })

    @model_validator(mode="after")
    def check_resolved_policy(self):
        if (
            self.pilot_candidate_count != self.candidate_count
            or self.max_llm_calls != _LLM_CALL_LIMITS[self.selection_policy]
        ):
            raise ValueError("Resolved limits do not match the v2 selection policy.")
        return self


class CreateJobV2(VersionedContract):
    input: JobInput
    policy: ExecutionPolicyInputV2
    llm_profile: ProfileReference | None = None
    search_profile: ProfileReference | None = None
    source_revision: Annotated[int, Field(ge=1)] | None = None
    catalog_hash: Sha256

    @model_validator(mode="after")
    def check_job_policy(self):
        policy = self.policy.selection_policy
        if policy == "catalog_fixed":
            if not isinstance(self.input, StructuredProblemInput) or self.llm_profile is not None:
                raise ValueError("Catalog fixed accepts only structured input and no language model profile.")
        elif self.llm_profile is None:
            raise PydanticCustomError("PROVIDER_CONFIGURATION_REQUIRED", "A language model profile is required.")
        if policy != "web_research_then_pilot" and (self.search_profile is not None or self.source_revision is not None):
            raise ValueError("Research settings references require a web research policy.")
        return self
