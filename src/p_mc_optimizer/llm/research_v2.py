"""Wire contracts for the catalog advisor and the search planner."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from p_mc_optimizer.domain.models import ContractModel, ProblemInput, ProblemProfile
from p_mc_optimizer.llm.models import Repair
from p_mc_optimizer.research.contracts import (
    AdvisorPlanV2, EvidencePassage, ResearchQueryPlan, SearchProblemProfile, SourceRule, SourceSnapshot,
)


class PlannerContext(ContractModel):
    profile: SearchProblemProfile
    sources: Annotated[list[SourceRule], Field(min_length=1, max_length=200)]
    max_queries: Annotated[int, Field(ge=1, le=3)]
    repair: Repair | None = None


class AdvisorContextV2(ContractModel):
    problem: ProblemInput
    profile: ProblemProfile
    solver_cards: Annotated[list[dict[str, Any]], Field(min_length=3, max_length=20)]
    evidence: Annotated[list[EvidencePassage], Field(max_length=12)]
    candidate_count: Literal[3]
    require_web: bool
    repair: Repair | None = None


SCHEMAS = {
    "research_planner.v2": ("research_planner", PlannerContext, ResearchQueryPlan),
    "advisor.v2": ("advisor", AdvisorContextV2, AdvisorPlanV2),
}


def validate_output(schema_id, output, context):
    if schema_id == "research_planner.v2":
        sources = SourceSnapshot(revision=1, sources=context.sources)
        if len(output.normalized(sources).queries) > context.max_queries:
            raise ValueError("Query count exceeds policy")
        return
    cards = {row["implementation_id"]: (row["implementation_id"], row["config_id"])
             for row in context.solver_cards}
    passages = {row.evidence_id: row.text for row in context.evidence}
    output.validate_references(cards, passages.keys(), require_web=context.require_web)
    for candidate in output.candidates:
        variant = candidate.implementation_id.rsplit(":", 1)[-1]
        if candidate.evidence_scope == "method_level" and not any(
            variant.casefold() in passages[ref].casefold() for ref in candidate.evidence_refs
        ):
            raise ValueError("Method-level evidence must identify the exact implementation variant")
    for finding in output.research_findings:
        match = finding.implementation_match
        if match is None:
            continue
        variant = match.implementation_id.rsplit(":", 1)[-1]
        refs = {ref for claim in finding.claims for ref in claim.evidence_refs}
        if finding.variant != variant or not any(variant.casefold() in passages[ref].casefold() for ref in refs):
            raise ValueError("Research finding must cite the exact implementation variant to claim a match")
