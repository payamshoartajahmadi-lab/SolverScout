"""Evidence-bound facts, strict plans and deterministic Persian report text."""
from copy import deepcopy
import json
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from p_mc_optimizer.llm.explanation_v2 import (
    ExplanationFactsV1, ExplainerContextV2, ExplainerPlanV2, build_explainer_context_v2,
    default_note_ids, explanation_facts_hash, validate_explainer_plan_v2,
)
from p_mc_optimizer.llm.formulation import RoleOutputError
from p_mc_optimizer.reporting import html_bytes
from p_mc_optimizer.reporting.explanation_v2 import (
    apply_explanation_v2, apply_invalid_context_explanation_v2, render_explanation_v2,
)
from test_explanation_context import DE, GWO, WOA, empirical_report, set_selection
from test_llm_gateway import _POLICY, checked, completion, golden, local, ready


@pytest.fixture
def report(empirical_report):
    empirical_report["best_x"] = [2.0, -1.0]
    empirical_report["explanation_status"] = "not_requested"
    return empirical_report


def context_for(report):
    return ExplainerContextV2.model_validate(build_explainer_context_v2(report))


def good_plan(context, **changes):
    values = {"schema_version": "2.0", "facts": context.facts.model_dump(mode="json"),
              "facts_hash": context.facts_hash, "note_ids": default_note_ids(context),
              "evidence_refs": list(context.evidence_refs)}
    values.update(changes)
    return values


def test_exp_03_valid_plan_uses_only_server_owned_text(report):
    before = deepcopy(report)
    context = context_for(report)
    plan = ExplainerPlanV2.model_validate(good_plan(context, note_ids=["verification_meaning", "median_meaning"]))
    validate_explainer_plan_v2(plan, context)
    apply_explanation_v2(report, context, plan, llm_schema_id="explainer.v2")
    assert report["best_x"] == before["best_x"] and report["budget"] == before["budget"]
    assert report["explanation_evidence"]["status"] == "consistent"
    assert report["explanation_evidence"]["note_selection_source"] == "llm"
    assert [note["note_id"] for note in report["explanation_notes"]] == plan.note_ids
    assert report["explanation"]["selection_explanation_fa"] == f"روش {DE} به دلیل کمترین میانهٔ امتیاز در پایلوت‌های معتبر انتخاب شد."
    assert set(report["explanation"]) == {"summary_fa", "selection_explanation_fa", "limitations_fa", "evidence_refs"}


@pytest.mark.parametrize("field,value", [
    pytest.param("selection_reason", "algorithm_id", id="EXP-04"),
    pytest.param("origin_phase", "pilot", id="EXP-06"),
    pytest.param("origin_algorithm_id", GWO, id="EXP-07-wrong-origin"),
    pytest.param("partial_result", True, id="EXP-08-changed-facts"),
])
def test_valid_schema_with_wrong_facts_is_rejected(report, field, value):
    context = context_for(report)
    values = good_plan(context)
    values["facts"][field] = value
    plan = ExplainerPlanV2.model_validate(values)
    with pytest.raises(RoleOutputError) as caught:
        validate_explainer_plan_v2(plan, context)
    assert any(error.startswith(f"facts.{field}:") for error in caught.value.field_errors)


def test_exp_08_wrong_hash_cannot_replace_matching_facts(report):
    context = context_for(report)
    plan = ExplainerPlanV2.model_validate(good_plan(context, facts_hash="f" * 64))
    with pytest.raises(RoleOutputError, match="Role output") as caught:
        validate_explainer_plan_v2(plan, context)
    assert any(error.startswith("facts_hash:") for error in caught.value.field_errors)


@pytest.mark.parametrize("free_field", ["summary_fa", "selection_explanation_fa", "limitations_fa", "text_fa"])
def test_exp_09_output_free_text_has_no_accepted_field(report, free_field):
    context = context_for(report)
    with pytest.raises(ValidationError):
        ExplainerPlanV2.model_validate(good_plan(context, **{free_field: "SENTINEL_MODEL_TEXT"}))


def test_historical_unknown_basis_free_text_cannot_be_a_v2_plan(report):
    context = context_for(report)
    historical = {
        "summary_fa": "بهترین مشاهدهٔ معتبر ثبت شد.",
        "selection_explanation_fa": "شواهد ارائه‌شده معیار یا مبنای این انتخاب را مشخص نمی‌کند.",
        "limitations_fa": ["مبنای تصمیم‌گیری در شواهد ارائه‌شده توضیح داده نشده است."],
        "evidence_refs": context.evidence_refs,
    }
    with pytest.raises(ValidationError):
        ExplainerPlanV2.model_validate(historical)
    apply_explanation_v2(report, context)
    assert "مبنا" not in report["explanation"]["selection_explanation_fa"]


@pytest.mark.parametrize("notes", [
    ["unregistered_note"], ["median_meaning", "median_meaning"], ["identifier_tiebreak"], [],
])
def test_exp_10_invalid_note_selection_is_rejected(report, notes):
    context = context_for(report)
    plan = ExplainerPlanV2.model_validate(good_plan(context, note_ids=notes))
    with pytest.raises(RoleOutputError) as caught:
        validate_explainer_plan_v2(plan, context)
    assert any(error.startswith("note_ids:") for error in caught.value.field_errors)


def test_exp_10_more_than_three_notes_is_rejected_by_closed_schema(report):
    context = context_for(report)
    with pytest.raises(ValidationError):
        ExplainerPlanV2.model_validate(good_plan(context, note_ids=["median_meaning"] * 4))


def not_reached_report():
    return {"selected_algorithm_id": None, "selection_status": "not_reached", "decision": None,
            "solution_status": "no_valid_solution", "solution_origin": None, "verification": None,
            "best_x": None, "objective_raw": None, "partial_result": False,
            "optimality_status": "not_certified", "budget": {}}


def test_exp_11_no_eligible_notes_requires_empty_selection():
    context = context_for(not_reached_report())
    assert context.allowed_notes == []
    validate_explainer_plan_v2(ExplainerPlanV2.model_validate(good_plan(context)), context)
    with pytest.raises(RoleOutputError):
        validate_explainer_plan_v2(ExplainerPlanV2.model_validate(good_plan(context, note_ids=["bounded_search"])), context)


@pytest.mark.parametrize("refs", [[], ["report:solution"], ["unknown"], ["report:solution"] * 2])
def test_exp_12_exact_complete_evidence_is_required(report, refs):
    context = context_for(report)
    with pytest.raises(RoleOutputError) as caught:
        validate_explainer_plan_v2(ExplainerPlanV2.model_validate(good_plan(context, evidence_refs=refs)), context)
    assert any(error.startswith("evidence_refs:") for error in caught.value.field_errors)


def test_exp_12_reference_order_can_change(report):
    context = context_for(report)
    validate_explainer_plan_v2(ExplainerPlanV2.model_validate(good_plan(context, evidence_refs=list(reversed(context.evidence_refs)))), context)


def test_exp_14_null_facts_keys_are_required_and_hashed():
    data = build_explainer_context_v2(not_reached_report())
    serialized = json.loads(json.dumps(data, ensure_ascii=False))
    assert serialized["facts"]["origin_algorithm_id"] is None
    assert serialized["facts"]["objective_sense"] is None
    assert data["facts_hash"] == explanation_facts_hash(serialized["facts"])
    del serialized["facts"]["origin_algorithm_id"]
    with pytest.raises(ValidationError):
        ExplainerContextV2.model_validate(serialized)


def start_empty_v2_explainer(client, repo):
    profile = ready(client)["profile"]
    created = checked(client.post("/jobs", json={
        "input": {"kind": "natural_language", "text": golden()["original_text"]},
        "policy": {**_POLICY, "explain_via_llm": True},
        "llm_profile": {key: profile[key] for key in ("profile_id", "revision")},
    }, headers={"Idempotency-Key": str(uuid4())}), 202)
    job_id = created["job_id"]
    owner = str(uuid4())
    assert repo.claim_job(owner)["job_id"] == job_id
    repo.update_job(job_id, phase="reporting", worker_id=owner)
    context = build_explainer_context_v2(not_reached_report())
    repo.put_workflow(job_id, "role_context:explainer", context, worker_id=owner)
    return {"job_id": job_id, "role": "explainer", "schema_id": "explainer.v2",
            "invocation_id": str(uuid4()), "context": context}


def test_exp_14_real_provider_message_preserves_nullable_facts(local):
    client, repo, calls, answers = local
    body = start_empty_v2_explainer(client, repo)
    context = ExplainerContextV2.model_validate(body["context"])
    answers.append(completion(good_plan(context)))
    outcome = checked(client.post("/internal/llm/invoke", json=body))
    assert outcome["status"] == "succeeded"
    message = json.loads(calls[-1]["body"]["messages"][-1]["content"])
    assert message == body["context"]
    assert message["facts"]["selected_algorithm_id"] is None
    assert message["facts"]["origin_algorithm_id"] is None
    assert message["facts"]["objective_sense"] is None
    assert message["report"]["decision_summary"] is None
    assert message["facts_hash"] == explanation_facts_hash(message["facts"])


def test_exp_13_added_note_is_rejected_before_api_reservation(local):
    client, repo, calls, _ = local
    body = start_empty_v2_explainer(client, repo)
    body["context"]["allowed_notes"].append({
        "note_id": "injected_note", "text_fa": "SENTINEL_INJECTED_NOTE", "evidence_refs": ["report:solution"],
    })
    error = checked(client.post("/internal/llm/invoke", json=body), 422)
    assert error["error_code"] == "INVALID_SCHEMA"
    assert len(calls) == 1  # The connection check is the only provider request.
    with repo.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM llm_calls WHERE job_id=?", (body["job_id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("reason", ["median_score", "normalized_iqr", "algorithm_id", "policy_assignment", "insufficient_evidence", "not_reached"])
def test_exp_16_all_selection_modes_have_the_prescribed_sentence(report, reason):
    if reason == "not_reached":
        report = not_reached_report()
    elif reason == "policy_assignment":
        report["selection_status"] = report["decision"]["selection_status"] = "fixed_by_policy"
        report["decision"].update(decision_source="llm_proposal", metrics=[], scale=None, tie_break_order=[])
    elif reason == "insufficient_evidence":
        for metric in report["decision"]["metrics"]:
            metric.update(eligible=False, median_score=None, normalized_iqr=None, reason="INELIGIBLE_PILOT_RESULT")
        set_selection(report, None, "insufficient_evidence")
    elif reason in {"normalized_iqr", "algorithm_id"}:
        for index, metric in enumerate(report["decision"]["metrics"]):
            metric.update(median_score=1.0, normalized_iqr=float(index) if reason == "normalized_iqr" else 0.0)
    context = context_for(report)
    assert context.facts.selection_reason == reason
    apply_explanation_v2(report, context)
    line = report["explanation"]["selection_explanation_fa"]
    if reason in {"insufficient_evidence", "not_reached"}:
        assert line is None
        assert ("کافی نبود" if reason == "insufficient_evidence" else "تکمیل نشد") in report["explanation"]["summary_fa"]
    else:
        assert line and DE in line
    if reason == "algorithm_id":
        assert "روش‌های صدر رتبه‌بندی برابر بود" in line and "ترتیب ثابت شناسه" in line


def test_exp_05_identifier_tie_rejects_a_median_plan(report):
    for metric in report["decision"]["metrics"]:
        metric.update(median_score=7.0, normalized_iqr=0.0)
    context = context_for(report)
    values = good_plan(context)
    values["facts"]["selection_reason"] = "median_score"
    with pytest.raises(RoleOutputError):
        validate_explainer_plan_v2(ExplainerPlanV2.model_validate(values), context)


def test_exp_17_maximization_keeps_negative_score_semantics(report):
    report["problem_revision"]["spec"]["objective"]["sense"] = "maximize"
    context = context_for(report)
    assert context.facts.score_semantics == "negated_objective"
    assert "maximization_scores" in [note.note_id for note in context.allowed_notes]
    apply_explanation_v2(report, context)
    assert "امتیاز مقایسه منفیِ مقدار تابع است" in report["explanation"]["selection_explanation_fa"]


@pytest.mark.parametrize("phase,label", [("preflight", "پیش‌پرواز"), ("profile", "نمونه‌برداری مسئله"), ("pilot", "پایلوت"), ("main", "اصلی")])
def test_exp_18_origin_uses_the_actual_phase_and_method(report, phase, label):
    algorithm = GWO if phase in {"pilot", "main"} else None
    report["solution_origin"] = {"phase": phase, "algorithm_id": algorithm}
    context = context_for(report)
    apply_explanation_v2(report, context)
    line = report["explanation"]["summary_fa"]
    assert label in line
    if algorithm:
        assert GWO in line
        assert "origin_differs" in [note.note_id for note in context.allowed_notes]


@pytest.mark.parametrize("status,verification", [("validation_failed", "failed"), ("validation_incomplete", "incomplete"), ("validation_incomplete", "passed")])
def test_exp_19_20_observed_unverified_points_are_not_called_valid_answers(report, status, verification):
    report.update(solution_status=status, partial_result=True)
    report["verification"]["validation_status"] = verification
    context = context_for(report)
    apply_explanation_v2(report, context)
    text = report["explanation"]["summary_fa"]
    assert "نقطهٔ مشاهده‌شده از اجرای اصلی" in text
    assert "این جواب" not in text and "بهترین مشاهدهٔ معتبرِ یافت‌شده" not in text
    assert context.facts.solution_status == status


@pytest.mark.parametrize("mutation", ["missing_point", "failed_verification", "unexpected_verification", "origin_method", "origin_stage", "pending_selection"])
def test_inconsistent_facts_fail_before_provider(report, mutation):
    if mutation == "missing_point":
        report["best_x"] = None
    elif mutation == "failed_verification":
        report["verification"]["validation_status"] = "failed"
    elif mutation == "unexpected_verification":
        report["verification"]["validation_status"] = "uncertain"
    elif mutation == "origin_method":
        report["solution_origin"]["algorithm_id"] = None
    elif mutation == "origin_stage":
        report["solution_origin"]["phase"] = "profile"
    else:
        report["selection_status"] = "pending"
    with pytest.raises(RoleOutputError) as caught:
        context_for(report)
    assert all(error.startswith("context:") for error in caught.value.field_errors)


def test_exp_23_invalid_context_has_no_comparative_or_origin_claim(report):
    original = deepcopy(report)
    apply_invalid_context_explanation_v2(report)
    assert report["best_x"] == original["best_x"] and report["budget"] == original["budget"]
    assert report["explanation_evidence"]["facts"] is None
    assert report["explanation_evidence"]["facts_hash"] is None
    assert report["explanation_evidence"]["note_selection_source"] == "none"
    assert report["explanation"]["selection_explanation_fa"] is None
    assert "شواهد سازگار و کافی" in report["explanation"]["summary_fa"]
    assert report["explanation_notes"] == []


def test_exp_21_22_default_notes_and_fallback_text_are_complete(report):
    context = context_for(report)
    apply_explanation_v2(report, context, llm_schema_id="explainer.v2")
    assert report["explanation_evidence"]["note_selection_source"] == "default"
    assert [note["note_id"] for note in report["explanation_notes"]] == default_note_ids(context)
    assert report["explanation_evidence"]["status"] == "consistent"
    assert report["explanation"]["summary_fa"]


def test_exp_25_invalid_output_never_enters_rendered_text(report):
    context = context_for(report)
    invalid = good_plan(context, summary_fa="SENTINEL_BAD_MODEL_OUTPUT")
    with pytest.raises(ValidationError):
        ExplainerPlanV2.model_validate(invalid)
    apply_explanation_v2(report, context)
    assert "SENTINEL_BAD_MODEL_OUTPUT" not in json.dumps(report["explanation"],ensure_ascii=False)
    assert "SENTINEL_BAD_MODEL_OUTPUT" not in json.dumps(report["explanation_notes"],ensure_ascii=False)


def test_exp_29_html_escapes_method_labels_exactly_once(report):
    context = context_for(report)
    report["solver_cards"] = [{"algorithm_id": DE, "class_path": "module.<script>alert(1)</script>"}]
    apply_explanation_v2(report, context)
    html = html_bytes(report).decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&amp;lt;script" not in html
    assert "توضیح مبتنی بر شواهد" in html and "نکات تکمیلی خودکار" in html
    assert "سازگاری شواهد" in html


def test_exp_28_old_html_remains_legacy_unchecked(report):
    report["explanation"] = {"summary_fa":"گزارش قدیمی", "limitations_fa":[]}
    html = html_bytes(report).decode()
    assert "legacy_unchecked" in html and "گزارش قدیمی" in html
    assert "توضیح مبتنی بر شواهد" not in html


@pytest.mark.parametrize("source,title", [("llm", "نکات تکمیلی انتخاب‌شده با مدل زبانی"), ("default", "نکات تکمیلی خودکار")])
def test_exp_30_ui_attributes_server_text_and_selected_notes_separately(report, monkeypatch, source, title):
    from test_library_v21_ui import _result_page

    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    context = context_for(report)
    plan = ExplainerPlanV2.model_validate(good_plan(context)) if source == "llm" else None
    apply_explanation_v2(report, context, plan, llm_schema_id="explainer.v2" if plan else None)
    report.update(job_id="recorded", job_status="completed", schema_version="1.0",
                  explanation_status="generated" if plan else "not_requested")
    page = AppTest.from_function(_result_page)
    page.session_state["recorded_report"] = report
    page.run()
    assert not page.exception
    assert any(item.value == "توضیح مبتنی بر شواهد" for item in page.subheader)
    assert any(item.value == title for item in page.subheader)
    assert all(item.value != "توضیح مدل زبانی" for item in page.caption)
    html = html_bytes(report).decode()
    assert title in html and "<h2>توضیح مبتنی بر شواهد</h2>" in html
