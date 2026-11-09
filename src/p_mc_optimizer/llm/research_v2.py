"""Wire contracts for the catalog advisor and the search planner."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from p_mc_optimizer.domain.models import ContractModel, ProblemInput, ProblemProfile
from p_mc_optimizer.llm.models import Repair
from p_mc_optimizer.research.contracts import (
    AdvisorPlanV2, EvidencePassage, ResearchQueryPlan, SearchProblemProfile, SourceRule,
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
