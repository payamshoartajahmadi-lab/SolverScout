"""Wire contracts for the catalog advisor and the search planner."""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import Field

from p_mc_optimizer.domain.models import ContractModel, DomainError, ProblemInput, ProblemProfile
from p_mc_optimizer.llm.formulation import RoleOutputError
from p_mc_optimizer.llm.models import Repair
from p_mc_optimizer.research.contracts import (
    AdvisorPlanV2, EvidencePassage, ResearchQueryPlan, SearchProblemProfile, SourceRule, SourceSnapshot,
)
from p_mc_optimizer.research.contracts_v21 import AdvisorPlanV21, EvidencePassageV21, ResearchQueryPlanV21, RetrievalMode


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


class PlannerCard(ContractModel):
    implementation_id: str
    family_id: str
    name: str
    aliases: list[str]


def planner_cards(cards):
    result = []
    for card in cards:
        name = card["implementation_id"].rsplit(":", 1)[-1]
        aliases = sorted({name, card["family_id"].upper()})
        result.append(PlannerCard(
            implementation_id=card["implementation_id"], family_id=card["family_id"],
            name=name, aliases=aliases,
        ))
    return result


class PlannerContextV21(PlannerContext):
    max_queries: Annotated[int, Field(ge=2, le=3)]
    retrieval_mode: RetrievalMode
    solver_cards: Annotated[list[PlannerCard], Field(min_length=3, max_length=20)]


class AdvisorContextV21(ContractModel):
    problem: ProblemInput
    profile: ProblemProfile
    solver_cards: Annotated[list[dict[str, Any]], Field(min_length=3, max_length=20)]
    evidence: Annotated[list[EvidencePassageV21], Field(max_length=12)]
    candidate_count: Literal[3]
    require_evidence: bool
    library_snapshot_id: str | None
    repair: Repair | None = None


SCHEMAS = {
    "research_planner.v2": ("research_planner", PlannerContext, ResearchQueryPlan),
    "advisor.v2": ("advisor", AdvisorContextV2, AdvisorPlanV2),
    "research_planner.v2.1": ("research_planner", PlannerContextV21, ResearchQueryPlanV21),
    "advisor.v2.1": ("advisor", AdvisorContextV21, AdvisorPlanV21),
}


def validate_output(schema_id, output, context):
    if schema_id.startswith("research_planner."):
        sources = SourceSnapshot(revision=1, sources=context.sources)
        web_plan = output.for_web() if isinstance(context, PlannerContextV21) else output
        if len(web_plan.normalized(sources).queries) > context.max_queries:
            raise ValueError("Query count exceeds policy")
        if isinstance(context, PlannerContextV21):
            allowed = {card.implementation_id for card in context.solver_cards}
            if any(set(query.implementation_ids) - allowed for query in output.queries):
                raise ValueError("The query names an implementation outside the catalog shortlist")
        return
    cards = {row["implementation_id"]: (row["implementation_id"], row["config_id"])
             for row in context.solver_cards}
    passages = {row.evidence_id: row.text for row in context.evidence}
    is_library = isinstance(context, AdvisorContextV21)
    require_evidence = context.require_evidence if is_library else context.require_web
    if is_library:
        try:
            output.validate_references(cards, passages.keys(), require_evidence=require_evidence)
        except DomainError as error:
            detail = str(error)
            if error.code == "IMPLEMENTATION_MAPPING_INVALID":
                detail += " Set card_ref to implementation_id. Copy config_id from the same solver card."
            raise RoleOutputError(detail) from None
    else:
        output.validate_references(cards, passages.keys(), require_web=require_evidence)
    def cites_variant(variant, refs):
        token = re.compile(r"(?<!\w)" + re.escape(variant) + r"(?!\w)", re.IGNORECASE)
        return any(token.search(passages[ref]) for ref in refs)

    if is_library:
        from p_mc_optimizer.research.library_retrieval import alias_present

        aliases = {card.implementation_id: card.aliases for card in planner_cards(context.solver_cards)}
    for candidate in output.candidates:
        variant = candidate.implementation_id.rsplit(":", 1)[-1]
        if candidate.evidence_scope == "method_level" and not cites_variant(variant, candidate.evidence_refs):
            raise ValueError("Method-level evidence must identify the exact implementation variant")
        if is_library and require_evidence:
            if candidate.evidence_scope == "family_level" and not any(
                alias_present(passages[ref], alias)
                for ref in candidate.evidence_refs for alias in aliases[candidate.implementation_id]
            ):
                raise ValueError("Family-level evidence must identify the proposed family")
    for finding in output.research_findings:
        match = finding.implementation_match
        if match is None:
            continue
        variant = match.implementation_id.rsplit(":", 1)[-1]
        refs = {ref for claim in finding.claims for ref in claim.evidence_refs}
        if finding.variant != variant or not cites_variant(variant, refs):
            raise ValueError("Research finding must cite the exact implementation variant to claim a match")
