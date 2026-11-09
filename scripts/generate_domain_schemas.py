"""Export schemas directly from the runtime domain contracts."""

from pathlib import Path

from p_mc_optimizer.domain.models import (
    CreateJob, EvaluationRecord, ExecutionPolicyInput, ExecutionResult, ProblemInput, ProblemRevision,
    SelectionDecision, TrialRequest,
)
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
}

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "schemas"
    for name, model in SCHEMAS.items():
        (root / f"{name}.v1.json").write_bytes(json_bytes(model.model_json_schema()) + b"\n")
    for name, model in SCHEMAS_V2.items():
        (root / f"{name}.v2.json").write_bytes(json_bytes(model.model_json_schema()) + b"\n")
