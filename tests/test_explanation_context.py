"""Contract tests for the decision evidence sent to the explainer."""

import json
import math
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from p_mc_optimizer.domain.models import ExecutionPolicy, ExecutionPolicyInput
from p_mc_optimizer.llm.explanation_context import build_decision_summary, build_explainer_context
from p_mc_optimizer.llm.formulation import RoleOutputError
from p_mc_optimizer.llm.models import ExplainerDecisionSummary
from p_mc_optimizer.research.contracts_v21 import ExecutionPolicyInputV21, ExecutionPolicyV21
from p_mc_optimizer.selection.policies import TIE_BREAK_ORDER


DE = "mealpy:evolutionary_based.DE:OriginalDE"
GWO = "mealpy:swarm_based.GWO:OriginalGWO"
WOA = "mealpy:swarm_based.WOA:OriginalWOA"
EVIDENCE_POLICY = "evidence_research_then_pilot"


@pytest.fixture
def empirical_report():
    policy = ExecutionPolicyV21.from_input(ExecutionPolicyInputV21(
        policy_version="2.1", portfolio_id="continuous_catalog_v2",
        selection_policy=EVIDENCE_POLICY, max_objective_evaluations=20000,
        max_compute_seconds=300, master_seed=1405, explain_via_llm=True,
    ))
    example = Path(__file__).resolve().parents[1] / "examples" / "quadratic_2d.json"
    problem = json.loads(example.read_text(encoding="utf-8"))
    metrics = [
        {
            "algorithm_id": algorithm, "eligible": True,
            "trial_ids": [f"00000000-0000-4000-8000-{index:012d}" for index in range(start, start + 3)],
            "median_score": median, "normalized_iqr": iqr, "reason": None,
        }
        for start, algorithm, median, iqr in (
            (1, DE, 0.0015692135159994156, 0.0008809686393022419),
            (4, GWO, 0.004693924447323589, 0.0022630692067125893),
            (7, WOA, 0.0024091725133136965, 0.0007204126979130296),
        )
    ]
    return {
        "problem_revision": {"spec": problem}, "policy": policy.model_dump(mode="json"),
        "requested_selection_policy": EVIDENCE_POLICY, "effective_selection_policy": EVIDENCE_POLICY,
        "decision": {
            "decision_source": "empirical_pilot_selection", "selection_status": "selected",
            "selected_algorithm_id": DE, "metrics": metrics,
            "scale": 1.0, "tie_break_order": TIE_BREAK_ORDER.copy(),
        },
        "selected_algorithm_id": DE, "selection_status": "selected",
        "solution_status": "valid_best_found", "solution_origin": {"phase": "main", "algorithm_id": DE},
        "verification": {"validation_status": "passed"}, "partial_result": False,
        "budget": {"known_started": 20000}, "optimality_status": "not_certified", "objective_raw": 0.0,
    }


def set_selection(report, selected, status="selected"):
    report.update(selected_algorithm_id=selected, selection_status=status)
    report["decision"].update(selected_algorithm_id=selected, selection_status=status)


def test_ctx_01_projects_exact_pilot_metrics(empirical_report):
    context = build_explainer_context(empirical_report)
    summary = context["report"]["decision_summary"]
    assert context["report"]["selected_algorithm_id"] == DE
    assert context["selection_made"]
    assert context["evidence_refs"] == ["report:solution", "report:verification", "report:budget", "report:decision"]
    assert summary == {
        "schema_version": "1.0", "mode": "empirical_pilots", "decision_source": "empirical_pilot_selection",
        "requested_selection_policy": EVIDENCE_POLICY, "effective_selection_policy": EVIDENCE_POLICY,
        "objective_sense": "minimize", "score_semantics": "raw_objective", "score_order": "lower_is_better",
        "pilot_repeats": 3, "pilot_evaluations": 500, "scale": 1.0, "tie_break_order": TIE_BREAK_ORDER,
        "metrics": empirical_report["decision"]["metrics"], "eligible_ranking": [DE, WOA, GWO],
        "decisive_criterion": "median_score",
    }
    assert "repair" not in context
    assert "decision_summary" not in empirical_report
    assert "problem_revision" not in context["report"]


def test_ctx_02_projection_is_repeatable_and_leaves_report_intact(empirical_report):
    before = deepcopy(empirical_report)
    context = build_explainer_context(empirical_report)
    assert context == build_explainer_context(empirical_report)
    context["report"]["decision_summary"]["metrics"][0]["trial_ids"].clear()
    context["report"]["budget"].clear()
    assert empirical_report == before


def test_ctx_03_maximize_uses_already_negated_scores(empirical_report):
    empirical_report["problem_revision"]["spec"]["objective"]["sense"] = "maximize"
    set_selection(empirical_report, GWO)
    for metric, score in zip(empirical_report["decision"]["metrics"], [-5.0, -9.0, -1.0], strict=True):
        metric["median_score"] = score
    summary = build_decision_summary(empirical_report)
    assert summary.objective_sense == "maximize"
    assert summary.score_semantics == "negated_objective"
    assert summary.eligible_ranking == [GWO, DE, WOA]
    assert [metric.median_score for metric in summary.metrics] == [-5.0, -9.0, -1.0]


@pytest.mark.parametrize("equal_iqr,winner,ranking,criterion", [
    pytest.param(False, WOA, [WOA, DE, GWO], "normalized_iqr", id="CTX-04"),
    pytest.param(True, DE, [DE, GWO, WOA], "algorithm_id", id="CTX-05"),
])
def test_tie_breaks_use_stored_values(empirical_report, equal_iqr, winner, ranking, criterion):
    for metric in empirical_report["decision"]["metrics"]:
        metric["median_score"] = 0.1
        if equal_iqr:
            metric["normalized_iqr"] = 0.05
    set_selection(empirical_report, winner)
    summary = build_decision_summary(empirical_report)
    assert summary.eligible_ranking == ranking
    assert summary.decisive_criterion == criterion


def test_ctx_04_close_medians_are_not_treated_as_ties(empirical_report):
    de, gwo, woa = empirical_report["decision"]["metrics"]
    de.update(median_score=0.1, normalized_iqr=0.05)
    gwo["median_score"] = 0.2
    woa.update(median_score=math.nextafter(0.1, 1.0), normalized_iqr=0.01)
    summary = build_decision_summary(empirical_report)
    assert summary.eligible_ranking == [DE, WOA, GWO]
    assert summary.decisive_criterion == "median_score"


def test_ctx_06_ineligible_metrics_keep_reason_and_trials(empirical_report):
    metric = empirical_report["decision"]["metrics"][1]
    metric.update(eligible=False, median_score=None, normalized_iqr=None, reason="INELIGIBLE_PILOT_RESULT")
    metric["private_history"] = ["not sent"]
    summary = build_decision_summary(empirical_report)
    assert summary.eligible_ranking == [DE, WOA]
    projected = summary.metrics[1].model_dump()
    assert projected == {key: value for key, value in metric.items() if key != "private_history"}


@pytest.mark.parametrize("eligible_count", [0, 1], ids=["CTX-07-none", "CTX-07-one"])
def test_insufficient_evidence_preserves_decision_without_selecting(empirical_report, eligible_count):
    set_selection(empirical_report, None, status="insufficient_evidence")
    for metric in empirical_report["decision"]["metrics"][eligible_count:]:
        metric.update(eligible=False, median_score=None, normalized_iqr=None, reason="INCOMPLETE_PILOT_SET")
    context = build_explainer_context(empirical_report)
    assert context["selection_made"] is False
    assert context["report"]["selected_algorithm_id"] is None
    assert "report:decision" in context["evidence_refs"]
    summary = context["report"]["decision_summary"]
    assert summary["mode"] == "empirical_pilots"
    assert summary["decisive_criterion"] is None
    assert summary["eligible_ranking"] == [DE][:eligible_count]
    assert summary["metrics"] == empirical_report["decision"]["metrics"]


@pytest.mark.parametrize("source", [
    pytest.param("llm_proposal", id="CTX-08"),
    pytest.param("deterministic_fallback", id="CTX-09"),
])
def test_llm_only_projects_policy_assignment_without_pilots(empirical_report, source):
    policy = ExecutionPolicy.from_input(ExecutionPolicyInput(
        policy_version="1.0", portfolio_id="continuous_v1", selection_policy="llm_only",
        fixed_algorithm_id=None, max_objective_evaluations=20000, max_compute_seconds=300,
        master_seed=1405, explain_via_llm=True,
    ))
    empirical_report["policy"] = policy.model_dump(mode="json")
    empirical_report.pop("requested_selection_policy")
    empirical_report["effective_selection_policy"] = None
    set_selection(empirical_report, "de.original", status="fixed_by_policy")
    empirical_report["decision"].update(decision_source=source, metrics=[], tie_break_order=[], scale=None)
    summary = build_decision_summary(empirical_report)
    assert summary.mode == "policy_assignment"
    assert summary.decision_source == source
    assert summary.requested_selection_policy == summary.effective_selection_policy == "llm_only"
    assert summary.metrics == summary.eligible_ranking == summary.tie_break_order == []
    assert summary.scale is summary.decisive_criterion is None
    assert (summary.pilot_repeats, summary.pilot_evaluations) == (3, 500)


def test_ctx_10_proposal_fallback_does_not_change_empirical_mode(empirical_report):
    empirical_report.update(proposal_source="deterministic_fallback", effective_selection_policy="catalog_llm_then_pilot")
    summary = build_decision_summary(empirical_report)
    assert summary.mode == "empirical_pilots"
    assert summary.decision_source == "empirical_pilot_selection"
    assert summary.requested_selection_policy == EVIDENCE_POLICY
    assert summary.effective_selection_policy == "catalog_llm_then_pilot"


def test_ctx_11_no_decision_needs_no_frozen_problem():
    context = build_explainer_context({})
    assert context["report"]["decision_summary"] is None
    assert context["selection_made"] is False
    assert context["evidence_refs"] == ["report:solution", "report:verification", "report:budget"]


@pytest.mark.parametrize("selected,status", [
    (GWO, "selected"), (None, "selected"), (None, "insufficient_evidence"), (DE, "fixed_by_policy"),
])
def test_ctx_12_rejects_mismatched_pilot_selection(empirical_report, selected, status):
    set_selection(empirical_report, selected, status=status)
    _assert_context_error(empirical_report)


@pytest.mark.parametrize("path,value", [
    (("selection_status",), "not_reached"),
    (("decision", "metrics", 0, "median_score"), float("inf")),
    (("decision", "metrics", 1, "algorithm_id"), DE),
    (("decision", "metrics", 0, "normalized_iqr"), -0.1),
    (("decision", "metrics", 0, "median_score"), None),
    (("decision", "metrics", 0, "trial_ids"), ["not-a-uuid"]),
    (("decision", "decision_source"), "unsupported_source"),
    (("problem_revision", "spec", "objective"), {}),
    (("decision", "scale"), 0.0),
    (("policy", "pilot_repeats"), -1),
    (("decision", "tie_break_order"), []),
])
def test_ctx_12_rejects_invalid_recorded_fields(empirical_report, path, value):
    node = empirical_report
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    _assert_context_error(empirical_report)


def _assert_context_error(report):
    before = deepcopy(report)
    with pytest.raises(RoleOutputError) as err:
        build_explainer_context(report)
    assert err.value.field_errors and all(message.startswith("context:") for message in err.value.field_errors)
    assert report == before


def test_ctx_13_origin_is_not_inferred_from_winning_solver(empirical_report):
    empirical_report["solution_origin"] = {"phase": "pilot", "algorithm_id": WOA}
    projected = build_explainer_context(empirical_report)["report"]
    assert projected["solution_origin"] == empirical_report["solution_origin"]
    assert projected["selected_algorithm_id"] == DE


def test_ctx_14_partial_result_is_visible(empirical_report):
    empirical_report["partial_result"] = True
    assert build_explainer_context(empirical_report)["report"]["partial_result"] is True


@pytest.mark.parametrize("target", ["summary", "metric"])
def test_ctx_15_decision_dtos_reject_extra_fields(empirical_report, target):
    payload = build_decision_summary(empirical_report).model_dump()
    node = payload if target == "summary" else payload["metrics"][0]
    node["extra"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExplainerDecisionSummary.model_validate(payload)


def test_ctx_16_selected_algorithm_requires_a_decision(empirical_report):
    empirical_report.pop("decision")
    assert build_decision_summary(empirical_report) is None
    _assert_context_error(empirical_report)
