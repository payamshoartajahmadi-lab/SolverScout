"""Export schemas directly from the runtime domain contracts."""

from pathlib import Path

from p_mc_optimizer.catalog.models import CatalogSnapshot, SolverCardV2
from p_mc_optimizer.domain.models import (
    CreateJob, EvaluationRecord, ExecutionPolicyInput, ExecutionResult, ProblemInput, ProblemRevision,
    SelectionDecision, TrialRequest, TrialRequestV2,
)
from p_mc_optimizer.reporting import json_bytes

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
    "catalog_snapshot": CatalogSnapshot,
    "solver_card": SolverCardV2,
    "trial_request": TrialRequestV2,
}

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "schemas"
    for version, contracts in ((1, SCHEMAS), (2, SCHEMAS_V2)):
        for name, model in contracts.items():
            (root / f"{name}.v{version}.json").write_bytes(json_bytes(model.model_json_schema()) + b"\n")
