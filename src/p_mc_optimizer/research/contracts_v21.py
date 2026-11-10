"""Strict contracts for the optional research routes and source library."""

from __future__ import annotations

from typing import Annotated, Literal, Mapping, Set

from pydantic import Field, model_validator

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
    FiniteReal,
    JobInput,
    ProfileReference,
)
from p_mc_optimizer.research.contracts import (
    AdvisorPlanV2,
    EvidencePassage,
    FrozenArray,
    Id,
    ImplementationId,
    ResearchFailurePolicy,
    ResearchLimits,
    ResearchQuery,
    ResearchQueryPlan,
    SearchProfile,
    Sha256,
    ShortId,
    SourceSnapshot,
    URL,
    VersionedContract,
)
from p_mc_optimizer.research.source_policy import SourcePolicy, SourcePolicyError, canonical_url

CATALOG_POLICY_VERSIONS = frozenset({"2.0", "2.1"})
RetrievalMode = Literal["local_only", "direct_refresh", "web_search"]
StaleEvidencePolicy = Literal["reject", "allow_with_warning"]
EvidenceAgeHours = Annotated[int, Field(ge=1, le=8760)]
Revision = Annotated[int, Field(ge=1)]
MIB = 1024 * 1024


class SyncLimits(ContractModel):
    max_sources: Annotated[int, Field(ge=1, le=10)] = 5
    max_urls_per_source: Annotated[int, Field(ge=1, le=1000)] = 200
    max_documents_per_source: Annotated[int, Field(ge=1, le=100)] = 30
    max_http_requests_per_source: Annotated[int, Field(ge=1, le=400)] = 120
    max_source_seconds: Annotated[FiniteReal, Field(gt=0, le=300)] = 120.0
    max_active_seconds: Annotated[FiniteReal, Field(gt=0, le=600)] = 300.0
    request_timeout_seconds: Annotated[FiniteReal, Field(gt=0, le=30)] = 20.0
    max_document_bytes: Annotated[int, Field(ge=1, le=10 * MIB)] = 5 * MIB
    max_sitemap_bytes: Annotated[int, Field(ge=1, le=5 * MIB)] = 2 * MIB
    max_total_bytes: Annotated[int, Field(ge=1, le=100 * MIB)] = 50 * MIB
    max_text_chars: Annotated[int, Field(ge=1, le=200_000)] = 100_000
    max_redirects: Annotated[int, Field(ge=0, le=3)] = 3


class LibrarySourceInputs(ContractModel):
    seed_urls: Annotated[FrozenArray[URL], Field(max_length=20)] = ()
    sitemap_urls: Annotated[FrozenArray[URL], Field(max_length=3)] = ()
    follow_links: bool = True
    crawl_depth: Annotated[int, Field(ge=0, le=2)] = 1
    limits: SyncLimits | None = None

    @model_validator(mode="after")
    def check_entrypoints(self):
        if not (self.seed_urls or self.sitemap_urls):
            raise ValueError("A source needs a seed URL or a registered sitemap.")
        urls = [canonical_url(url)[0] for url in (*self.seed_urls, *self.sitemap_urls)]
        if len(set(urls)) != len(urls):
            raise ValueError("Source entrypoints must have distinct canonical URLs.")
        return self


class LibrarySourceConfig(LibrarySourceInputs):
    schema_version: Literal["2.1"] = "2.1"
    source_id: ShortId
    revision: Revision
    source_revision: Revision

    def check_source(self, snapshot: SourceSnapshot) -> LibrarySourceConfig:
        policy = SourcePolicy(snapshot)
        rule = next((rule for rule in snapshot.active_sources if rule.source_id == self.source_id), None)
        if rule is None:
            raise SourcePolicyError("Source is missing or disabled.")
        seeds = tuple(policy.match(url, (self.source_id,)).url for url in self.seed_urls)
        sitemaps = []
        for url in self.sitemap_urls:
            canonical, hostname, _ = canonical_url(url)
            if hostname != rule.hostname:
                raise SourcePolicyError("Registered sitemaps must use the source hostname.")
            sitemaps.append(canonical)
        return self.model_copy(update={"seed_urls": seeds, "sitemap_urls": tuple(sitemaps)})


class LibrarySourceUpdate(LibrarySourceInputs):
    expected_revision: Annotated[int, Field(ge=0)]

    def to_config(self, source_id: str, source_revision: int) -> LibrarySourceConfig:
        values = self.model_dump(exclude={"expected_revision"})
        return LibrarySourceConfig(
            source_id=source_id, source_revision=source_revision,
            revision=self.expected_revision + 1, **values,
        )


class LibrarySourceRef(ContractModel):
    source_id: ShortId
    revision: Revision


class SourceSyncRequest(ContractModel):
    source_ids: Annotated[FrozenArray[ShortId], Field(min_length=1, max_length=10)]
    limits: SyncLimits = SyncLimits()

    @model_validator(mode="after")
    def check_sources(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Sync source IDs must be distinct.")
        if len(self.source_ids) > self.limits.max_sources:
            raise ValueError("Source selection exceeds the sync limit.")
        return self


class RetrievalSettings(ContractModel):
    retrieval_mode: RetrievalMode = "local_only"
    max_evidence_age_hours: EvidenceAgeHours = 168
    stale_evidence_policy: StaleEvidencePolicy = "reject"
    on_research_failure: ResearchFailurePolicy = "stop"
    provider_id: ShortId | None = None


class ExecutionPolicyInputV21(ContractModel):
    policy_version: Literal["2.1"]
    portfolio_id: Literal["continuous_catalog_v2"]
    selection_policy: Literal["evidence_research_then_pilot"]
    max_objective_evaluations: Annotated[int, Field(ge=5502, le=1_000_000)]
    max_compute_seconds: Annotated[int, Field(ge=11, le=3600)]
    master_seed: Annotated[int, Field(ge=0, lt=2**32)]
    explain_via_llm: bool
    retrieval_mode: RetrievalMode = "local_only"
    max_evidence_age_hours: EvidenceAgeHours = 168
    stale_evidence_policy: StaleEvidencePolicy = "reject"
    on_research_failure: ResearchFailurePolicy = "stop"
    sync_limits: SyncLimits | None = None

    @model_validator(mode="after")
    def check_mode(self):
        if (self.sync_limits is not None) != (self.retrieval_mode == "direct_refresh"):
            raise ValueError("Sync limits are required only for direct refresh.")
        return self

    @property
    def candidate_count(self) -> int:
        return 3

    @property
    def main_evaluations(self) -> int:
        return (
            self.max_objective_evaluations - PREFLIGHT_EVALUATIONS - PROFILE_EVALUATIONS
            - self.candidate_count * PILOT_REPEATS * PILOT_EVALUATIONS - VERIFICATION_EVALUATIONS
        )


class ExecutionPolicyV21(ExecutionPolicyInputV21):
    preflight_evaluations: Literal[8] = PREFLIGHT_EVALUATIONS
    profile_evaluations: Literal[492] = PROFILE_EVALUATIONS
    pilot_repeats: Literal[3] = PILOT_REPEATS
    pilot_evaluations: Literal[500] = PILOT_EVALUATIONS
    verification_evaluations: Literal[2] = VERIFICATION_EVALUATIONS
    verification_time_reserve_seconds: Literal[10] = VERIFICATION_TIME_RESERVE_SECONDS
    max_llm_calls: Literal[8] = 8
    max_llm_calls_per_role: Literal[2] = 2
    max_trial_attempts: Literal[1] = 1
    numeric_atol: Literal[1e-12] = NUMERIC_ATOL
    numeric_rtol: Literal[1e-10] = NUMERIC_RTOL
    use_history: Literal[False] = False
    pilot_candidate_count: Literal[3] = 3

    @classmethod
    def from_input(cls, policy: ExecutionPolicyInputV21) -> ExecutionPolicyV21:
        return cls.model_validate(policy.model_dump())


class CreateJobV21(VersionedContract):
    schema_version: Literal["2.1"] = "2.1"
    input: JobInput
    policy: ExecutionPolicyInputV21
    llm_profile: ProfileReference
    catalog_hash: Sha256
    source_revision: Revision
    source_config_refs: Annotated[FrozenArray[LibrarySourceRef], Field(max_length=300)] = ()
    search_provider_ref: ProfileReference | None = None

    @model_validator(mode="after")
    def check_mode(self):
        if (self.search_provider_ref is not None) != (self.policy.retrieval_mode == "web_search"):
            raise ValueError("A search provider is required only for web search.")
        if len({ref.source_id for ref in self.source_config_refs}) != len(self.source_config_refs):
            raise ValueError("Source configuration IDs must be distinct.")
        if self.policy.retrieval_mode == "direct_refresh":
            if not self.source_config_refs:
                raise ValueError("Direct refresh requires registered source configurations.")
            if len(self.source_config_refs) > self.policy.sync_limits.max_sources:
                raise ValueError("Source selection exceeds the sync limit.")
        return self


class ResearchSnapshotV21(VersionedContract):
    schema_version: Literal["2.1"] = "2.1"
    policy_version: Literal["2.1"] = "2.1"
    retrieval_mode: RetrievalMode
    source_snapshot: SourceSnapshot
    search_profile: SearchProfile | None = None
    source_configs: FrozenArray[LibrarySourceConfig] = ()
    catalog_hash: Sha256
    max_evidence_age_hours: EvidenceAgeHours = 168
    stale_evidence_policy: StaleEvidencePolicy = "reject"
    on_research_failure: ResearchFailurePolicy = "stop"
    sync_limits: SyncLimits | None = None
    planner_prompt_version: Literal["research_planner.v2.1"] = "research_planner.v2.1"
    advisor_prompt_version: Literal["advisor.v2.1"] = "advisor.v2.1"

    @model_validator(mode="after")
    def check_mode(self):
        if (self.search_profile is not None) != (self.retrieval_mode == "web_search"):
            raise ValueError("A search profile is required only for web search.")
        if (self.sync_limits is not None) != (self.retrieval_mode == "direct_refresh"):
            raise ValueError("Sync limits are required only for direct refresh.")
        if self.retrieval_mode == "direct_refresh" and not self.source_configs:
            raise ValueError("Direct refresh requires source configurations.")
        return self


class ResearchQueryV21(ResearchQuery):
    implementation_ids: Annotated[FrozenArray[ImplementationId], Field(max_length=20)] = ()

    @model_validator(mode="after")
    def check_targets(self):
        if (self.purpose == "implementation_evidence") != bool(self.implementation_ids):
            raise ValueError("Implementation evidence requires catalog targets.")
        if len(set(self.implementation_ids)) != len(self.implementation_ids):
            raise ValueError("Catalog targets must be distinct.")
        return self


class ResearchQueryPlanV21(VersionedContract):
    schema_version: Literal["2.1"]
    queries: Annotated[FrozenArray[ResearchQueryV21], Field(min_length=2, max_length=3)]

    @model_validator(mode="after")
    def check_queries(self):
        if len({query.query_id for query in self.queries}) != len(self.queries):
            raise ValueError("Query IDs must be distinct.")
        if len({(query.text, frozenset(query.source_ids), query.purpose) for query in self.queries}) != len(self.queries):
            raise ValueError("Query text, sources and purpose must be distinct as a group.")
        if {query.purpose for query in self.queries} != {"implementation_evidence", "method_discovery"}:
            raise ValueError("A plan requires both implementation evidence and method discovery.")
        return self

    def for_web(self) -> ResearchQueryPlan:
        return ResearchQueryPlan(
            schema_version="2.0",
            queries=tuple(
                ResearchQuery.model_validate(query.model_dump(exclude={"implementation_ids"}))
                for query in self.queries
            ),
        )

    def normalized(self, source_snapshot: SourceSnapshot, limits: ResearchLimits | None = None):
        self.for_web().normalized(source_snapshot, limits)
        return self


class AdvisorPlanV21(AdvisorPlanV2):
    schema_version: Literal["2.1"]

    def validate_references(
        self,
        cards: Mapping[str, tuple[str, str]],
        evidence_ids: Set[str],
        *,
        require_evidence: bool,
    ) -> None:
        super().validate_references(cards, evidence_ids, require_web=require_evidence)


class EvidencePassageV21(EvidencePassage):
    schema_version: Literal["2.1"] = "2.1"
    version_id: Id
    observation_id: Id
    source_id: ShortId
    url: URL
    selection_method: Literal["implementation_evidence", "method_discovery", "local_search"]
    stale: bool = False
