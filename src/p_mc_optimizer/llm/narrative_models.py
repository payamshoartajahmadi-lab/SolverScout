"""Closed, versioned narrative contracts; no execution or provider dependencies."""
from __future__ import annotations

from typing import Annotated, Literal, Self
from math import isfinite
from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator
from p_mc_optimizer.domain.base import ContractModel

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ParagraphId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
SectionId = Literal["problem", "candidate_rationale", "selection", "solution", "verification_and_limits", "mathematical_analysis"]
SECTION_ORDER = ("problem", "candidate_rationale", "selection", "solution", "verification_and_limits", "mathematical_analysis")
REQUIRED_SECTIONS = SECTION_ORDER[:5]
SECTION_IDS = SECTION_ORDER
REQUIRED_SECTION_IDS = REQUIRED_SECTIONS
IssueCode = Literal[
    "SCHEMA_INVALID", "CONTEXT_TOO_LARGE", "CONTEXT_MISMATCH", "REQUEST_MISMATCH", "DRAFT_MISMATCH",
    "UNKNOWN_FACT", "UNKNOWN_EVIDENCE", "INVALID_BINDING", "UNBOUND_NUMBER", "MISSING_REQUIRED_SECTION",
    "MISSING_REQUIRED_FACT", "OUT_OF_SCOPE_REPAIR", "UNSUPPORTED_CLAIM", "CONTRADICTS_EVIDENCE",
    "WRONG_SELECTION_REASON", "WRONG_SOLUTION_ORIGIN", "WRONG_OBJECTIVE_SEMANTICS", "WRONG_BUDGET_SCOPE",
    "FALSE_VERIFICATION", "FALSE_CERTIFICATION", "UNSUPPORTED_CAUSALITY", "UNSUPPORTED_GENERALIZATION",
    "SOURCE_NOT_SUPPORTING", "INSUFFICIENT_SPECIFICITY", "REDUNDANT_CONTENT", "UNSAFE_MARKUP",
]
FactValue = StrictFloat | StrictInt | StrictStr | StrictBool | list[StrictFloat | StrictInt] | None


class DerivationRecord(ContractModel):
    operation: Literal["count", "sum", "median", "iqr_linear", "negate", "subtract", "relative_decrease_percent", "bounds_membership", "format_group"]
    input_fact_refs: list[str]
    definition_version: Literal["1.0"]


class FactRecord(ContractModel):
    fact_id: str
    value_type: Literal["number", "integer", "text", "boolean", "number_vector"]
    value: FactValue
    availability: Literal["available", "missing"]
    unit: str | None
    display_text: str
    source_paths: list[str]
    evidence_refs: list[str]
    origin: Literal["recorded", "derived", "mathematical_constant"]
    derivation: DerivationRecord | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.availability == "missing":
            if self.value is not None or self.display_text != "ثبت نشده":
                raise ValueError("Missing facts require null and the explicit missing label")
        else:
            types = {"number": (int, float), "integer": (int,), "text": (str,), "boolean": (bool,), "number_vector": (list,)}
            if type(self.value) not in types[self.value_type]:
                raise ValueError("Fact value must have its exact declared type; booleans are not numbers")
            numbers = self.value if isinstance(self.value, list) else [self.value]
            if self.value_type in {"number", "integer", "number_vector"} and any(type(x) not in (int, float) or not isfinite(x) for x in numbers):
                raise ValueError("Every numerical fact must be finite")
            if self.fact_id.endswith(".evaluation_counts") and any(type(x) is not int or x < 0 for x in numbers):
                raise ValueError("Evaluation count components must be nonnegative integers")
        if self.origin == "derived" and self.derivation is None:
            raise ValueError("Derived facts require their operation and inputs")
        if self.origin != "derived" and self.derivation is not None:
            raise ValueError("Only a derived fact may declare a derivation")
        if self.origin == "mathematical_constant" and (type(self.value) not in (int, float) or self.value not in (0, 1, 2) or self.availability != "available" or self.source_paths or self.evidence_refs):
            raise ValueError("Only explicit zero, one and two are mathematical constants")
        return self


class EvidenceRecord(ContractModel):
    evidence_id: str
    kind: Literal["run_record", "source_passage", "derived_calculation"]
    locator: str
    text: str
    content_sha256: Sha256
    original_content_sha256: Sha256 | None
    excerpt_start: int | None
    excerpt_end: int | None
    source_url: str | None
    source_title: str | None
    scope: Literal["run_specific", "family_level", "implementation_level"]
    stale: bool | None

    @model_validator(mode="after")
    def excerpt_contract(self) -> Self:
        from hashlib import sha256
        if sha256(self.text.encode("utf-8")).hexdigest() != self.content_sha256:
            raise ValueError("Evidence content hash does not match the transmitted text")
        if self.kind == "source_passage":
            if self.original_content_sha256 is None or type(self.excerpt_start) is not int or type(self.excerpt_end) is not int or self.excerpt_start < 0 or self.excerpt_end - self.excerpt_start != len(self.text):
                raise ValueError("A source excerpt needs Unicode character offsets and its original passage hash")
        elif any(x is not None for x in (self.original_content_sha256, self.excerpt_start, self.excerpt_end)):
            raise ValueError("Run and calculation records do not have passage excerpt metadata")
        return self


class MethodIdentity(ContractModel):
    alias: Annotated[str, Field(pattern=r"^m[1-9][0-9]*$")]
    implementation_id: str
    display_name_fa: str


class SectionRequirement(ContractModel):
    section_id: SectionId
    required: bool
    required_fact_refs: list[str]
    allowed_evidence_refs: list[str]
    requirement_text_fa: str


class OmissionRecord(ContractModel):
    item_ref: str
    reason: Literal["size_limit", "missing", "not_relevant"]


class ReportEvidenceV3(ContractModel):
    schema_version: Literal["3.0"]
    job_id: str
    source_report_schema_version: str
    source_core_sha256: Sha256
    facts: list[FactRecord]
    sources: list[EvidenceRecord]
    sections: list[SectionRequirement]
    methods: list[MethodIdentity]
    omissions: list[OmissionRecord]
    evidence_policy_version: Literal["1.0"]

    @model_validator(mode="after")
    def unique_resolvable_identifiers(self) -> Self:
        for values in ([x.fact_id for x in self.facts], [x.evidence_id for x in self.sources], [x.section_id for x in self.sections], [x.alias for x in self.methods], [x.implementation_id for x in self.methods]):
            if len(values) != len(set(values)):
                raise ValueError("Evidence identifiers must be unique")
        facts, sources = {x.fact_id for x in self.facts}, {x.evidence_id for x in self.sources}
        for fact in self.facts:
            if set(fact.evidence_refs) - sources or (fact.derivation and set(fact.derivation.input_fact_refs) - facts):
                raise ValueError("Fact refers to an absent source or derivation input")
        for section in self.sections:
            if set(section.required_fact_refs) - facts or set(section.allowed_evidence_refs) - sources:
                raise ValueError("Section requirements must resolve in this exact evidence")
        return self


class ValidationIssueV1(ContractModel):
    section_id: SectionId | None
    paragraph_id: str | None
    code: IssueCode
    evidence_refs: list[str]
    message_fa: str
    required_change_fa: str


class NarrativeParagraphV3(ContractModel):
    paragraph_id: ParagraphId
    claim_kind: Literal["recorded", "derived", "interpretation"]
    text_fa: Annotated[str, Field(min_length=1, max_length=2200)]
    fact_refs: list[str]
    evidence_refs: list[str]


class NarrativeSectionV3(ContractModel):
    section_id: SectionId
    paragraphs: Annotated[list[NarrativeParagraphV3], Field(max_length=3)]


class WriterContextV3(ContractModel):
    schema_version: Literal["3.0"]
    mode: Literal["draft", "repair"]
    prompt_bundle_sha256: Sha256
    evidence: ReportEvidenceV3
    context_hash: Sha256
    request_hash: Sha256
    section_scope: list[SectionId]
    accepted_sections: list[NarrativeSectionV3]
    failed_sections: list[NarrativeSectionV3]
    previous_response_text: Annotated[str, Field(max_length=8192)] | None
    previous_response_truncated: bool
    repair_issues: list[ValidationIssueV1]


class NarrativeDraftV3(ContractModel):
    schema_version: Literal["3.0"]
    context_hash: Sha256
    request_hash: Sha256
    sections: list[NarrativeSectionV3]


class RenderedParagraphV1(ContractModel):
    paragraph_id: str
    text_fa: str


class RenderedSectionV1(ContractModel):
    section_id: SectionId
    paragraphs: list[RenderedParagraphV1]


class SectionHashV1(ContractModel):
    section_id: SectionId
    sha256: Sha256


class ReviewerContextV1(ContractModel):
    schema_version: Literal["1.0"]
    prompt_bundle_sha256: Sha256
    evidence: ReportEvidenceV3
    context_hash: Sha256
    request_hash: Sha256
    draft_hash: Sha256
    draft_sections: list[NarrativeSectionV3]
    rendered_sections: list[RenderedSectionV1]
    section_scope: list[SectionId]
    accepted_section_hashes: list[SectionHashV1]


class SectionReviewV1(ContractModel):
    section_id: SectionId
    verdict: Literal["accept", "reject"]
    issues: list[ValidationIssueV1]


class NarrativeReviewV1(ContractModel):
    schema_version: Literal["1.0"]
    context_hash: Sha256
    request_hash: Sha256
    draft_hash: Sha256
    section_reviews: list[SectionReviewV1]


class AcceptedSectionV3(ContractModel):
    section: NarrativeSectionV3
    origin: Literal["llm", "server"]
    source_draft_hash: Sha256 | None
    reviewer_invocation_id: str | None


class NarrativeAttemptV1(ContractModel):
    role: Literal["explainer", "report_reviewer"]
    schema_id: Literal["explainer.v3", "narrative_review.v1"]
    operation: Literal["draft", "repair", "review", "review_retry"]
    invocation_id: str
    call_id: str | None
    request_hash: Sha256
    draft_hash: Sha256 | None
    status: Literal["succeeded", "failed", "unknown", "cancelled"]
    error_code: str | None


class NarrativeV3(ContractModel):
    schema_version: Literal["3.0"]
    mode: Literal["llm", "mixed", "deterministic"]
    evidence_status: Literal["valid", "invalid"]
    context_hash: Sha256 | None
    source_core_sha256: Sha256 | None
    evidence: ReportEvidenceV3 | None
    accepted_sections: list[AcceptedSectionV3]
    rendered_sections: list[RenderedSectionV1]
    review_status: Literal["passed", "partial", "failed", "not_run"]
    omitted_sections: list[SectionId]
    issues: list[ValidationIssueV1]
    attempts: list[NarrativeAttemptV1]
    renderer_version: Literal["3.0.0"]
    formatter_version: Literal["1.0"]
    evidence_builder_version: Literal["1.0"]
    elapsed_reporting_seconds: Annotated[float, Field(ge=0)]
