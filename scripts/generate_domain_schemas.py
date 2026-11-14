"""Export schemas directly from the runtime domain contracts."""

from pathlib import Path

from p_mc_optimizer.catalog.models import CatalogSnapshot, SolverCardV2
from p_mc_optimizer.domain.models import (
    CreateJob, EvaluationRecord, ExecutionPolicyInput, ExecutionResult, ProblemInput, ProblemRevision,
    SelectionDecision, SelectionDecisionV2, TrialRequest, TrialRequestV2,
)
from p_mc_optimizer.llm.narrative_models import NarrativeDraftV3, NarrativeReviewV1, ReportEvidenceV3, ReviewerContextV1_1
from p_mc_optimizer.llm.models import provider_schema
from p_mc_optimizer.reporting import json_bytes
from p_mc_optimizer.research.contracts import (
    AdvisorPlanV2, CreateJobV2, EvidencePassage, ExecutionPolicyInputV2, ResearchOutcome,
    ResearchQueryPlan, ResearchSnapshot, SearchHit, SearchProblemProfile, SearchProfile,
    SearchRequest, SourceDocument, SourceRule, SourceSnapshot,
)

SCHEMAS = {
    "create_job": CreateJob,
    "evaluation_record": EvaluationRecord,
    "execution_policy_input": ExecutionPolicyInput,
    "execution_result": ExecutionResult,
    "problem_input": ProblemInput,
    "problem_revision": ProblemRevision,
    "selection_decision": SelectionDecision,
    "trial_request": TrialRequest,
}

SCHEMAS_V2 = {
    "advisor_plan": AdvisorPlanV2,
    "create_job": CreateJobV2,
    "evidence_passage": EvidencePassage,
    "execution_policy_input": ExecutionPolicyInputV2,
    "research_outcome": ResearchOutcome,
    "research_query_plan": ResearchQueryPlan,
    "research_snapshot": ResearchSnapshot,
    "search_hit": SearchHit,
    "search_problem_profile": SearchProblemProfile,
    "search_profile": SearchProfile,
    "search_request": SearchRequest,
    "source_document": SourceDocument,
    "source_rule": SourceRule,
    "source_snapshot": SourceSnapshot,
    "catalog_snapshot": CatalogSnapshot,
    "selection_decision": SelectionDecisionV2,
    "solver_card": SolverCardV2,
    "trial_request": TrialRequestV2,
}

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "schemas"
    for version, contracts in ((1, SCHEMAS), (2, SCHEMAS_V2)):
        for name, model in contracts.items():
            (root / f"{name}.v{version}.json").write_bytes(json_bytes(model.model_json_schema()) + b"\n")


    for name, model in {"explainer.v3": NarrativeDraftV3, "narrative_review.v1": NarrativeReviewV1, "report_evidence.v3": ReportEvidenceV3,
                        "narrative_review_context.v1.1": ReviewerContextV1_1}.items():
        (root / (name + ".json")).write_bytes(json_bytes(provider_schema(model)) + b"\n")
