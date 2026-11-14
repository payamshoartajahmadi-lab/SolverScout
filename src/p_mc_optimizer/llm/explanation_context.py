"""Project the recorded decision and result into the explainer's input."""
from __future__ import annotations

from copy import deepcopy

from pydantic import ValidationError

from p_mc_optimizer.llm.formulation import RoleOutputError
from p_mc_optimizer.llm.models import ExplainerDecisionSummary, ExplainerPilotMetric
from p_mc_optimizer.selection.policies import TIE_BREAK_ORDER


_RESULT_FIELDS = (
    "selected_algorithm_id", "selection_status", "solution_status", "solution_origin",
    "verification", "partial_result", "budget", "optimality_status", "objective_raw",
)


def build_decision_summary(report: dict) -> ExplainerDecisionSummary | None:
    decision = report.get("decision")
    if decision is None:
        return None
    try:
        return _project_decision(report, decision, report.get("selected_algorithm_id"))
    except ValidationError as error:
        raise RoleOutputError(*(
            f"context: decision_summary {'.'.join(map(str, detail['loc']))} {detail['msg']}"
            for detail in error.errors(include_input=False, include_url=False)
        )) from None
    except (KeyError, TypeError):
        raise RoleOutputError("context: recorded decision or problem is missing required data") from None


def _project_decision(report: dict, decision: dict, selected: str | None) -> ExplainerDecisionSummary:
    source = decision["decision_source"]
    status = decision["selection_status"]
    if report["selection_status"] != status or decision["selected_algorithm_id"] != selected:
        raise RoleOutputError("context: recorded decision and report selection differ")

    raw_metrics = decision["metrics"]
    if not isinstance(raw_metrics, list) or any(not isinstance(row, dict) for row in raw_metrics):
        raise RoleOutputError("context: recorded pilot metrics must be a list of objects")
    metrics = [
        ExplainerPilotMetric.model_validate(
            {key: row[key] for key in ExplainerPilotMetric.model_fields}
        )
        for row in raw_metrics
    ]
    tie_break_order = decision["tie_break_order"]
    ranking = []
    criterion = None
    if source == "empirical_pilot_selection":
        mode = "empirical_pilots"
        if tie_break_order != TIE_BREAK_ORDER:
            raise RoleOutputError("context: unexpected pilot tie-break order")
        ranked = sorted(
            (metric for metric in metrics if metric.eligible),
            key=lambda metric: tuple(getattr(metric, field) for field in TIE_BREAK_ORDER),
        )
        ranking = [metric.algorithm_id for metric in ranked]
        winner = ranking[0] if len(ranking) >= 2 else None
        expected_status = "selected" if winner is not None else "insufficient_evidence"
        if selected != winner or status != expected_status:
            raise RoleOutputError("context: recorded selection does not match eligible pilot ranking")
        if winner is not None:
            first, second = ranked[:2]
            criterion = next(
                (field for field in TIE_BREAK_ORDER if getattr(first, field) != getattr(second, field)),
                None,
            )
    elif status == "fixed_by_policy":
        mode = "policy_assignment"
        if not isinstance(selected, str) or not selected.strip() or metrics or tie_break_order:
            raise RoleOutputError("context: policy assignment requires a selected method without pilot metrics")
    else:
        raise RoleOutputError("context: unsupported decision source or selection status")

    policy = report["policy"]
    requested = report.get("requested_selection_policy")
    effective = report.get("effective_selection_policy")
    sense = report["problem_revision"]["spec"]["objective"]["sense"]
    return ExplainerDecisionSummary(
        schema_version="1.0",
        mode=mode,
        decision_source=source,
        requested_selection_policy=policy["selection_policy"] if requested is None else requested,
        effective_selection_policy=policy["selection_policy"] if effective is None else effective,
        objective_sense=sense,
        score_semantics="raw_objective" if sense == "minimize" else "negated_objective",
        score_order="lower_is_better",
        pilot_repeats=policy["pilot_repeats"],
        pilot_evaluations=policy["pilot_evaluations"],
        scale=decision["scale"],
        tie_break_order=tie_break_order,
        metrics=metrics,
        eligible_ranking=ranking,
        decisive_criterion=criterion,
    )


def build_explainer_context(report: dict) -> dict:
    decision = build_decision_summary(report)
    summary = deepcopy({key: report.get(key) for key in _RESULT_FIELDS})
    selection_made = summary["selected_algorithm_id"] is not None
    if selection_made and decision is None:
        raise RoleOutputError("context: selected algorithm has no recorded decision")
    summary["decision_summary"] = decision.model_dump(mode="json") if decision is not None else None
    evidence = ["report:solution", "report:verification", "report:budget"]
    if decision is not None:
        evidence.append("report:decision")
    return {
        "report": summary,
        "evidence_refs": evidence,
        "selection_made": selection_made,
    }
