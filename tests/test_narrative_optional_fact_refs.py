"""Frozen Windows U1 references and report-only normalization regressions.

The archived run remains a policy-1.2 live failure. These tests replay its raw
writer structures offline; prescribed reviewer verdicts are not a quality eval.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from p_mc_optimizer.llm.narrative_models import WriterContextV3
from p_mc_optimizer.llm.narrative_policy import freeze_narrative_policy
from p_mc_optimizer.reporting.evidence_v3 import core_hash, evidence_hash, value_hash
from p_mc_optimizer.reporting.narrative_metadata import contextual_binding_feedback, normalize_optional_fact_refs
from p_mc_optimizer.reporting.narrative_validation import TOKEN, draft_hash, validate_draft
from p_mc_optimizer.workflow.narrative_v3 import NarrativeOrchestrator
from test_narrative_closure_workflow import ClosureDriver
from test_narrative_v3_workflow import BUNDLE, correct_draft, review_response


RECORDED = Path(__file__).parent / "fixtures/narrative_v3/U1_windows_unused_optional_fact_ref.json"


@pytest.fixture(autouse=True)
def offline_report_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Optional-reference regressions cannot dispatch HTTP or numerical work")

    for target in (
        "httpx.HTTPTransport.handle_request",
        "p_mc_optimizer.llm.gateway.LLMGateway.invoke",
        "p_mc_optimizer.execution.evaluator.BudgetedEvaluator.__call__",
        "p_mc_optimizer.execution.supervisor.run_execution",
    ):
        monkeypatch.setattr(target, forbidden)


def recorded():
    return json.loads(RECORDED.read_text(encoding="utf-8"))


def attempt(mode):
    source = recorded()
    item = next(value for value in source["attempts"] if value["mode"] == mode)
    context = WriterContextV3.model_validate({
        **item["writer_context_without_evidence"], "evidence": source["evidence"],
    })
    return context, item["raw_draft"]


def section(raw, identifier):
    return next(item for item in raw["sections"] if item["section_id"] == identifier)


def candidate_paragraph(raw):
    return next(item for item in section(raw, "candidate_rationale")["paragraphs"]
                if item["paragraph_id"] == "candidate_reasons")


def issue_keys(checked):
    return {(item.code, item.section_id, item.paragraph_id) for item in checked.issues}


def test_new_policy_is_explicit_and_does_not_change_the_historical_frozen_definition():
    source = recorded()
    old = freeze_narrative_policy("1.2")
    new = freeze_narrative_policy("1.3")
    assert old["report_generation_policy_hash"] == source["historical_generation_policy_hash"]
    assert old["roles"]["explainer.v3"]["prompt_name"] == "explainer.v3.1"
    assert new["roles"]["explainer.v3"]["prompt_name"] == "explainer.v3.2"
    assert old["report_generation_policy_hash"] != new["report_generation_policy_hash"]
    assert old["evidence_policy_version"] == new["evidence_policy_version"] == "1.1"
    assert old["roles"]["narrative_review.v1"] == new["roles"]["narrative_review.v1"]


@pytest.mark.parametrize("mode", ["draft", "repair"])
def test_recorded_original_and_repair_reproduce_strict_historical_failure(mode):
    source = recorded()
    saved = next(item for item in source["attempts"] if item["mode"] == mode)
    context, raw = attempt(mode)
    before = deepcopy(raw)
    assert source["historical_live_result"] == "FAIL"
    assert source["historical_generation_policy_version"] == "1.2"
    assert value_hash(raw) == saved["raw_draft_sha256"]
    assert evidence_hash(context.evidence) == context.context_hash
    paragraph = candidate_paragraph(raw)
    assert set(paragraph["fact_refs"]) - set(TOKEN.findall(paragraph["text_fa"])) == {"problem.expression"}
    requirements = {item.section_id: item for item in context.evidence.sections}
    assert "problem.expression" in requirements["problem"].required_fact_refs
    assert "problem.expression" not in requirements["candidate_rationale"].required_fact_refs
    checked = validate_draft(context, raw)
    assert [item.model_dump(mode="json") for item in checked.issues] == saved["historical_validation_issues"]
    assert ("INVALID_BINDING", "candidate_rationale", "candidate_reasons") in issue_keys(checked)
    assert raw == before
    if mode == "draft":
        assert ("MISSING_REQUIRED_FACT", "selection", None) in issue_keys(checked)
        assert {item.section_id for item in checked.valid_sections} == {
            "problem", "solution", "verification_and_limits",
        }
    else:
        assert {item.section_id for item in checked.valid_sections} == {"selection"}


@pytest.mark.parametrize("mode", ["draft", "repair"])
@pytest.mark.parametrize("policy_version", ["1.0", "1.1", "1.2"])
def test_historical_policies_never_normalize_recorded_writer_metadata(mode, policy_version):
    context, raw = attempt(mode)
    effective, audit = normalize_optional_fact_refs(context, raw, policy_version=policy_version)
    assert effective == raw and effective is not raw and audit is None
    assert ("INVALID_BINDING", "candidate_rationale", "candidate_reasons") in issue_keys(validate_draft(context, effective))


@pytest.mark.parametrize("mode", ["draft", "repair"])
def test_new_policy_removes_only_exact_optional_metadata_and_is_idempotent(mode):
    context, raw = attempt(mode)
    before, frozen_context = deepcopy(raw), context.model_dump(mode="json")
    effective, audit = normalize_optional_fact_refs(context, raw, policy_version="1.3")
    expected = deepcopy(raw)
    candidate_paragraph(expected)["fact_refs"].remove("problem.expression")
    assert effective == expected and raw == before
    assert context.model_dump(mode="json") == frozen_context
    assert audit["status"] == "applied"
    assert audit["raw_response_sha256"] == value_hash(raw)
    assert audit["effective_response_sha256"] == value_hash(effective)
    assert audit["raw_response_sha256"] != audit["effective_response_sha256"]
    assert audit["removed_fact_refs"] == [{
        "section_id": "candidate_rationale", "paragraph_id": "candidate_reasons",
        "fact_id": "problem.expression", "fact_ref_index": 3,
    }]
    assert audit["text_changed"] is False and audit["evidence_refs_changed"] is False
    checked = validate_draft(context, effective)
    assert "candidate_rationale" in {item.section_id for item in checked.valid_sections}
    if mode == "draft":
        assert issue_keys(checked) == {
            ("INVALID_BINDING", "selection", "selection_pilots"),
            ("MISSING_REQUIRED_FACT", "selection", None),
        }
        assert section(effective, "selection") == section(raw, "selection")
    else:
        assert checked.valid and {item.section_id for item in checked.valid_sections} == {"candidate_rationale", "selection"}
    repeated, repeat_audit = normalize_optional_fact_refs(context, effective, policy_version="1.3")
    assert repeated == effective and repeat_audit["status"] == "unchanged"
    assert repeat_audit["removed_fact_refs"] == []
    assert repeat_audit["raw_response_sha256"] == repeat_audit["effective_response_sha256"]


@pytest.mark.parametrize("defect,expected_code", [
    ("unknown_ref", "UNKNOWN_FACT"),
    ("unknown_token", "UNKNOWN_FACT"),
    ("unlisted_token", "INVALID_BINDING"),
    ("duplicate_used_ref", "INVALID_BINDING"),
    ("duplicate_optional_ref", "INVALID_BINDING"),
    ("broken_token", "INVALID_BINDING"),
    ("raw_number", "UNBOUND_NUMBER"),
    ("unknown_evidence", "UNKNOWN_EVIDENCE"),
    ("invalid_claim_kind", "SCHEMA_INVALID"),
    ("missing_mandatory_token", "MISSING_REQUIRED_FACT"),
])
def test_normalization_cannot_rescue_binding_grounding_or_prose_defects(defect, expected_code):
    context, raw = attempt("repair")
    paragraph = candidate_paragraph(raw)
    if defect == "unknown_ref":
        paragraph["fact_refs"].append("foreign_job.result")
    elif defect == "unknown_token":
        paragraph["text_fa"] += " {{fact:foreign_job.result}}"
        paragraph["fact_refs"].append("foreign_job.result")
    elif defect == "unlisted_token":
        paragraph["fact_refs"].remove("method.m1.label")
    elif defect == "duplicate_used_ref":
        paragraph["fact_refs"].append("method.m1.label")
    elif defect == "duplicate_optional_ref":
        paragraph["fact_refs"].append("problem.expression")
    elif defect == "broken_token":
        paragraph["text_fa"] += " {{fact:problem.expression}"
    elif defect == "raw_number":
        paragraph["text_fa"] += " 314159"
    elif defect == "unknown_evidence":
        paragraph["evidence_refs"] = ["source:foreign_job"]
    elif defect == "invalid_claim_kind":
        paragraph["claim_kind"] = "certified_by_model"
    elif defect == "missing_mandatory_token":
        origin = section(raw, "candidate_rationale")["paragraphs"][0]
        origin["text_fa"] = origin["text_fa"].replace("{{fact:proposal.methods}}", "")
    before = deepcopy(raw)
    effective, audit = normalize_optional_fact_refs(context, raw, policy_version="1.3")
    checked = validate_draft(context, effective)
    assert expected_code in {item.code for item in checked.issues}
    assert "candidate_rationale" not in {item.section_id for item in checked.valid_sections}
    assert raw == before
    for original_section, effective_section in zip(raw["sections"], effective["sections"], strict=True):
        for original, changed in zip(original_section["paragraphs"], effective_section["paragraphs"], strict=True):
            assert original["text_fa"] == changed["text_fa"]
            assert original["evidence_refs"] == changed["evidence_refs"]
    assert audit["text_changed"] is False and audit["evidence_refs_changed"] is False
    if defect in {"unknown_ref", "unknown_token", "unlisted_token", "duplicate_used_ref", "duplicate_optional_ref"}:
        assert effective == raw and audit["status"] == "ineligible"


@pytest.mark.parametrize("defect,expected_code", [
    ("request_hash", "REQUEST_MISMATCH"),
    ("context_hash", "CONTEXT_MISMATCH"),
    ("scope", "OUT_OF_SCOPE_REPAIR"),
])
def test_normalization_does_not_change_an_unbound_or_out_of_scope_response(defect, expected_code):
    context, raw = attempt("repair")
    if defect == "scope":
        _, original = attempt("draft")
        raw["sections"].append(deepcopy(section(original, "problem")))
    else:
        raw[defect] = "0" * 64
    before = deepcopy(raw)
    effective, audit = normalize_optional_fact_refs(context, raw, policy_version="1.3")
    checked = validate_draft(context, effective)
    assert checked.global_error and expected_code in {item.code for item in checked.issues}
    assert effective == raw == before and audit["status"] == "ineligible"


def test_required_section_reference_is_retained_even_when_another_paragraph_covers_it():
    context, raw = attempt("repair")
    candidate_paragraph(raw)["fact_refs"].append("proposal.methods")
    effective, audit = normalize_optional_fact_refs(context, raw, policy_version="1.3")
    paragraph = candidate_paragraph(effective)
    assert "problem.expression" not in paragraph["fact_refs"]
    assert "proposal.methods" in paragraph["fact_refs"]
    checked = validate_draft(context, effective)
    assert ("INVALID_BINDING", "candidate_rationale", "candidate_reasons") in issue_keys(checked)
    assert not any(item.code == "MISSING_REQUIRED_FACT" for item in checked.issues)
    assert [item["fact_id"] for item in audit["removed_fact_refs"]] == ["problem.expression"]


@pytest.mark.parametrize("malformed", [None, "invalid", {}, [None], [{"section_id": [], "paragraphs": None}]])
def test_malformed_containers_remain_ineligible_without_breaking_metadata_or_feedback(malformed):
    context, raw = attempt("repair")
    raw["sections"] = deepcopy(malformed)
    before = deepcopy(raw)
    effective, audit = normalize_optional_fact_refs(context, raw, policy_version="1.3")
    assert effective == raw == before and audit["status"] == "ineligible"
    checked = validate_draft(context, effective)
    assert not checked.valid
    feedback = contextual_binding_feedback(context, effective, checked.issues)
    assert [item.model_dump(mode="json") for item in feedback] == [item.model_dump(mode="json") for item in checked.issues]


def with_optional_candidate_reference(context):
    raw = correct_draft(context)
    candidate = section(raw, "candidate_rationale")["paragraphs"][0]
    candidate["fact_refs"] = [*candidate["fact_refs"], "problem.expression"]
    return raw


def assert_effective_review_binding(context):
    assert context["draft_hash"] == draft_hash(context["context_hash"], context["draft_sections"])
    for item in context["draft_sections"]:
        for paragraph in item["paragraphs"]:
            assert set(paragraph["fact_refs"]) == set(TOKEN.findall(paragraph["text_fa"]))


def test_optional_candidate_repair_keeps_accepted_sections_and_effective_reviewer_hash():
    driver = ClosureDriver()
    driver.writer = with_optional_candidate_reference
    driver.reviewer = lambda ctx: review_response(
        ctx, reject=["candidate_rationale"] if not ctx["accepted_section_hashes"] else [],
    )
    before = core_hash(driver.report)
    result = driver.run(version="1.3")
    assert result["mode"] == "llm" and result["review_status"] == "passed"
    assert driver.used == {"explainer": 2, "report_reviewer": 2}
    draft_call, first_review, repair_call, final_review = driver.sent
    assert repair_call["context"]["section_scope"] == ["candidate_rationale"]
    assert len(repair_call["context"]["accepted_sections"]) == 4
    final = {item["section"]["section_id"]: item for item in result["accepted_sections"]}
    for accepted in repair_call["context"]["accepted_sections"]:
        assert final[accepted["section_id"]]["section"] == accepted
    for marker in final_review["context"]["accepted_section_hashes"]:
        assert marker["sha256"] == value_hash(final[marker["section_id"]]["section"])
    for sent in (first_review, final_review):
        assert_effective_review_binding(sent["context"])
    for call, reviewed in ((draft_call, first_review), (repair_call, final_review)):
        raw = driver.results[call["invocation_id"]]["output"]
        raw_candidate = section(raw, "candidate_rationale")
        effective_candidate = next(item for item in reviewed["context"]["draft_sections"]
                                   if item["section_id"] == "candidate_rationale")
        assert raw_candidate["paragraphs"][0]["fact_refs"][-1] == "problem.expression"
        assert effective_candidate["paragraphs"][0]["fact_refs"] == raw_candidate["paragraphs"][0]["fact_refs"][:-1]
        assert effective_candidate["paragraphs"][0]["text_fa"] == raw_candidate["paragraphs"][0]["text_fa"]
        assert effective_candidate["paragraphs"][0]["evidence_refs"] == raw_candidate["paragraphs"][0]["evidence_refs"]
        assert value_hash(raw_candidate) != value_hash(effective_candidate)
    assert final["candidate_rationale"]["source_draft_hash"] == final_review["context"]["draft_hash"]
    assert all(item["context"]["evidence"] == draft_call["context"]["evidence"] for item in driver.sent)
    assert driver.journal["calls"]["draft"]["result"] == driver.results[draft_call["invocation_id"]]
    assert driver.journal["calls"]["repair"]["result"] == driver.results[repair_call["invocation_id"]]
    for key, sent in (("draft", draft_call), ("repair", repair_call)):
        persisted = driver.journal["calls"][key]
        raw = driver.results[sent["invocation_id"]]["output"]
        expected, audit = normalize_optional_fact_refs(sent["context"], raw, policy_version="1.3")
        assert persisted["effective_output"] == expected
        assert {key: persisted["metadata_normalization"][key] for key in audit} == audit
        assert audit["raw_response_sha256"] == value_hash(persisted["result"]["output"])
        assert audit["effective_response_sha256"] == value_hash(persisted["effective_output"])
        assert audit["removed_fact_refs"] and audit["status"] == "applied"
    assert core_hash(driver.report) == before
    frozen_calls = deepcopy(driver.sent)
    assert driver.run(version="1.3") == result and driver.sent == frozen_calls


def test_a_new_caller_cannot_upgrade_or_normalize_a_frozen_policy_12_journal():
    driver = ClosureDriver()
    driver.writer = with_optional_candidate_reference
    old_result = driver.run(version="1.2")
    assert old_result["mode"] == "mixed"
    old_journal, old_sent = deepcopy(driver.journal), deepcopy(driver.sent)
    assert driver.run(version="1.2") == old_result and driver.sent == old_sent
    saved = []

    def forbidden(*args, **kwargs):
        pytest.fail("A frozen legacy journal cannot dispatch calls under a new caller policy")

    engine = NarrativeOrchestrator(
        driver.report, invoke=forbidden, persist=lambda value: saved.append(deepcopy(value)),
        journal=old_journal, prompt_bundle_sha256=BUNDLE, report_generation_policy_version="1.3",
    )
    result = engine.run()
    assert engine.report_generation_policy_version == "1.2"
    assert engine.generation_policy == freeze_narrative_policy("1.2")
    assert result["mode"] == "deterministic"
    assert any(item["code"] == "CONTEXT_MISMATCH" for item in result["issues"])
    assert old_journal == driver.journal
    assert saved and all(item["report_generation_policy_version"] == "1.2" for item in saved)
    assert all(item["calls"] == old_journal["calls"] for item in saved)
    assert all("effective_output" not in item and "metadata_normalization" not in item
               for item in old_journal["calls"].values())


def test_optional_candidate_reference_does_not_hide_mandatory_selection_omission():
    driver = ClosureDriver()

    def writer(context):
        raw = with_optional_candidate_reference(context) if context["mode"] == "draft" else correct_draft(context)
        if context["mode"] == "draft":
            paragraph = section(raw, "selection")["paragraphs"][0]
            paragraph["text_fa"] = paragraph["text_fa"].replace("{{fact:selection.decisive_criterion}}", "")
        return raw

    driver.writer = writer
    result = driver.run(version="1.3")
    assert result["mode"] == "llm" and driver.used == {"explainer": 2, "report_reviewer": 2}
    assert "candidate_rationale" in driver.sent[1]["context"]["section_scope"]
    assert "selection" not in driver.sent[1]["context"]["section_scope"]
    repair = driver.sent[2]["context"]
    assert repair["section_scope"] == ["selection"]
    assert any(item["code"] == "MISSING_REQUIRED_FACT" and item["section_id"] == "selection"
               for item in repair["repair_issues"])
    frozen_candidate = next(item for item in repair["accepted_sections"] if item["section_id"] == "candidate_rationale")
    assert next(item["section"] for item in result["accepted_sections"]
                if item["section"]["section_id"] == "candidate_rationale") == frozen_candidate


def test_normalization_cannot_accept_a_claim_rejected_by_the_independent_reviewer():
    """A prescribed semantic verdict survives normalization and bounded repair."""
    driver = ClosureDriver()
    false_claim = " این تنظیمات برای همهٔ مسائل دارای گواهی قطعی بهینه بودن هستند."

    def writer(context):
        raw = with_optional_candidate_reference(context)
        section(raw, "candidate_rationale")["paragraphs"][0]["text_fa"] += false_claim
        return raw

    def reviewer(context):
        candidate = next(item for item in context["draft_sections"] if item["section_id"] == "candidate_rationale")
        assert candidate["paragraphs"][0]["text_fa"].endswith(false_claim)
        assert "problem.expression" not in candidate["paragraphs"][0]["fact_refs"]
        response = review_response(context, reject=["candidate_rationale"])
        rejected = next(item for item in response["section_reviews"] if item["section_id"] == "candidate_rationale")
        rejected["issues"][0].update(code="FALSE_CERTIFICATION", evidence_refs=["report:proposal"])
        return response

    driver.writer, driver.reviewer = writer, reviewer
    result = driver.run(version="1.3")
    assert driver.used == {"explainer": 2, "report_reviewer": 2}
    assert result["mode"] == "mixed"
    candidate = next(item for item in result["accepted_sections"] if item["section"]["section_id"] == "candidate_rationale")
    assert candidate["origin"] == "server"
    assert any(item["code"] == "FALSE_CERTIFICATION" for item in result["issues"])
    assert all(false_claim not in paragraph["text_fa"] for item in result["rendered_sections"] for paragraph in item["paragraphs"])
