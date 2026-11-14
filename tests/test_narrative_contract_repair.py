"""Synthetic repair regression fixtures; never recovered historical C-L1 responses.

Writers/reviewers below are controlled Python fakes. These tests prove contract
handling and request construction, not live model quality or historical replay.
"""
from copy import deepcopy
from hashlib import sha256
import json

import httpx
import pytest

from p_mc_optimizer.llm.models import provider_schema
from p_mc_optimizer.llm.narrative_models import NarrativeDraftV3, WriterContextV3
from p_mc_optimizer.llm.provider import build_request, prepare_provider_request
from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3, canonical_bytes, core_hash
from p_mc_optimizer.reporting.narrative_validation import request_hash, section_hash, validate_draft
from p_mc_optimizer.workflow.narrative_v3 import build_writer_context
from test_narrative_closure_policy import snapshot
from test_narrative_closure_workflow import ClosureDriver
from test_narrative_v3_workflow import BUNDLE, correct_draft, fixture


ID_PATTERN = r"^[a-z][a-z0-9_]{0,47}$"


@pytest.fixture(autouse=True)
def reporting_only_boundaries(monkeypatch):
    """Any accidental real transport or numerical work must fail this module."""
    from p_mc_optimizer.execution import evaluator, supervisor

    def forbidden(*args, **kwargs):
        pytest.fail("Synthetic contract repair must not dispatch HTTP or numerical work")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(evaluator.BudgetedEvaluator, "__call__", forbidden)
    monkeypatch.setattr(supervisor, "run_execution", forbidden)


def writer_context(version="1.1"):
    return build_writer_context(build_evidence_v3(fixture(), policy_version=version),
                                prompt_bundle_sha256=BUNDLE)


def as_dict(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else deepcopy(value)


def paragraph(raw, section="problem"):
    return next(s for s in raw["sections"] if s["section_id"] == section)["paragraphs"][0]


def synthetic_invalid_draft(context, *, all_sections=False):
    raw = correct_draft(as_dict(context))
    for section in raw["sections"]:
        if all_sections or section["section_id"] == "problem":
            section["paragraphs"][0]["paragraph_id"] = section["section_id"] + "-main"
    return raw


def prepared_repair():
    context = writer_context()
    invalid = synthetic_invalid_draft(context)
    checked = validate_draft(context, invalid)
    repair = build_writer_context(
        context.evidence, prompt_bundle_sha256=BUNDLE, scope=["problem"],
        accepted_sections=checked.valid_sections, failed_sections=checked.invalid_sections,
        repair_issues=checked.issues,
    )
    return context, invalid, checked, repair


def test_invalid_id_has_actionable_field_diagnostic_and_preserved_synthetic_section():
    context = writer_context()
    invalid = synthetic_invalid_draft(context)
    untouched = deepcopy(invalid)
    result = validate_draft(context, invalid)
    assert not result.global_error
    assert {s.section_id for s in result.valid_sections} == {
        "candidate_rationale", "selection", "solution", "verification_and_limits"
    }
    assert len(result.invalid_sections) == 1
    rejected = as_dict(result.invalid_sections[0])
    assert rejected["payload_kind"] == "invalid_section"
    assert rejected["section_id"] == "problem"
    assert rejected["paragraphs"][0]["paragraph_index"] == 0
    for key in ("paragraph_id", "text_fa", "claim_kind", "fact_refs", "evidence_refs"):
        assert rejected["paragraphs"][0][key] == paragraph(invalid)[key]
    error = next(e for e in rejected["structural_errors"]
                 if e["field_path"].endswith(".paragraphs[0].paragraph_id"))
    assert error["error_type"] == "string_pattern_mismatch"
    assert ID_PATTERN in error["expected_rule"]
    assert "problem_main" in error["repair_instruction_fa"]
    schema_issue = next(i for i in result.issues if i.code == "SCHEMA_INVALID")
    diagnostic = schema_issue.message_fa + " " + schema_issue.required_change_fa
    assert schema_issue.section_id == "problem"
    assert error["field_path"] in diagnostic and ID_PATTERN in diagnostic
    assert "problem_main" in diagnostic
    assert invalid == untouched


def test_invalid_projection_bounds_large_data_and_omits_unknown_sensitive_fields():
    context = writer_context()
    invalid = synthetic_invalid_draft(context)
    section = invalid["sections"][0]
    section["headers"] = {"Authorization": "Bearer SYNTHETIC_HEADER_SECRET"}
    section["api_key"] = "SYNTHETIC_KEY_SECRET"
    section["unrelated_private_payload"] = {"value": "SYNTHETIC_PRIVATE_SECRET"}
    paragraph(invalid)["unrelated_field"] = "SYNTHETIC_PARAGRAPH_SECRET"
    paragraph(invalid)["text_fa"] += "x" * 50_000
    untouched = deepcopy(invalid)
    result = validate_draft(context, invalid)
    assert not result.global_error and len(result.invalid_sections) == 1
    rejected = as_dict(result.invalid_sections[0])
    serialized = json.dumps(rejected, ensure_ascii=False)
    assert rejected["truncated"] and len(canonical_bytes(rejected)) <= 16_384
    assert len(rejected["paragraphs"]) <= 3 and len(rejected["structural_errors"]) <= 12
    for secret in ("SYNTHETIC_HEADER_SECRET", "SYNTHETIC_KEY_SECRET",
                   "SYNTHETIC_PRIVATE_SECRET", "SYNTHETIC_PARAGRAPH_SECRET"):
        assert secret not in serialized
        assert secret not in json.dumps([as_dict(i) for i in result.issues], ensure_ascii=False)
    assert "{{fact:problem.sense}}" in rejected["paragraphs"][0]["text_fa"]
    assert invalid == untouched


def test_invalid_projection_redacts_recognizable_inline_credentials_without_mutating_raw():
    context = writer_context()
    invalid = synthetic_invalid_draft(context)
    paragraph(invalid)["text_fa"] += " Authorization: Bearer SYNTHETIC_REPAIR_SECRET_123456789"
    untouched = deepcopy(invalid)
    result = validate_draft(context, invalid)
    public = json.dumps([as_dict(s) for s in result.invalid_sections]
                        + [as_dict(i) for i in result.issues], ensure_ascii=False)
    assert "SYNTHETIC_REPAIR_SECRET_123456789" not in public
    assert invalid == untouched


@pytest.mark.parametrize("fragment,secret", [
    ('{"api_key": "SYNTHETIC_QUOTED_API_SECRET"}', "SYNTHETIC_QUOTED_API_SECRET"),
    ('{"cookie": "session=SYNTHETIC_QUOTED_COOKIE_SECRET"}', "SYNTHETIC_QUOTED_COOKIE_SECRET"),
    ('{"api_\x00key": "SYNTHETIC_CONTROL_KEY_SECRET"}', "SYNTHETIC_CONTROL_KEY_SECRET"),
])
def test_invalid_projection_redacts_quoted_json_secrets_after_control_normalization(fragment, secret):
    """Synthetic key names include a real control character, not recovered data."""
    context = writer_context()
    invalid = synthetic_invalid_draft(context)
    paragraph(invalid)["text_fa"] += " " + fragment
    untouched = deepcopy(invalid)
    checked = validate_draft(context, invalid)
    assert len(checked.invalid_sections) == 1
    public = json.dumps([as_dict(s) for s in checked.invalid_sections]
                        + [as_dict(i) for i in checked.issues], ensure_ascii=False)
    assert secret not in public
    retained = as_dict(checked.invalid_sections[0])["paragraphs"][0]["text_fa"]
    assert "{{fact:problem.sense}}" in retained and "\x00" not in retained
    assert invalid == untouched


@pytest.mark.parametrize("public_contract", [
    "numeric_execution.authorization = synthetic_public_execution_contract",
    '{"numeric_execution.authorization": "synthetic_public_execution_contract"}',
])
def test_invalid_projection_preserves_public_numeric_execution_authorization(public_contract):
    context = writer_context()
    invalid = synthetic_invalid_draft(context)
    paragraph(invalid)["text_fa"] += " " + public_contract
    untouched = deepcopy(invalid)
    checked = validate_draft(context, invalid)
    assert len(checked.invalid_sections) == 1
    retained = as_dict(checked.invalid_sections[0])["paragraphs"][0]["text_fa"]
    assert public_contract in retained
    assert invalid == untouched


@pytest.mark.parametrize("keep_unused_ref", [False, True])
def test_valid_id_does_not_hide_missing_problem_sense_token(keep_unused_ref):
    context = writer_context()
    invalid = synthetic_invalid_draft(context)
    paragraph(invalid)["text_fa"] = paragraph(invalid)["text_fa"].replace(
        "{{fact:problem.sense}}", "کمینه‌سازی")
    if not keep_unused_ref:
        paragraph(invalid)["fact_refs"].remove("problem.sense")
    structural = validate_draft(context, invalid)
    assert any(i.code == "SCHEMA_INVALID" for i in structural.issues)
    paragraph(invalid)["paragraph_id"] = "problem_main"
    before = deepcopy(invalid)
    checked = validate_draft(context, invalid)
    codes = {i.code for i in checked.issues if i.section_id == "problem"}
    assert "SCHEMA_INVALID" not in codes and "MISSING_REQUIRED_FACT" in codes
    assert ("INVALID_BINDING" in codes) is keep_unused_ref
    missing = next(i for i in checked.issues if i.code == "MISSING_REQUIRED_FACT")
    assert "problem.sense" in missing.message_fa and "problem.sense" in missing.required_change_fa
    assert invalid == before


def test_corrected_structural_section_passes_with_snapshot_and_frozen_accepted_hashes():
    _, invalid, _, repair = prepared_repair()
    hashes = {s.section_id: section_hash(s) for s in repair.accepted_sections}
    assert repair.mode == "repair" and len(repair.failed_sections) == 1
    assert as_dict(repair.failed_sections[0])["paragraphs"][0]["text_fa"] == paragraph(invalid)["text_fa"]
    corrected = correct_draft(as_dict(repair))
    paragraph(corrected)["paragraph_id"] = "problem_main"
    before = deepcopy(corrected)
    result = validate_draft(repair, corrected)
    assert not result.global_error and not result.issues
    assert [s.section_id for s in result.valid_sections] == ["problem"]
    assert {s.section_id: section_hash(s) for s in repair.accepted_sections} == hashes
    assert corrected == before and request_hash(repair) == repair.request_hash


def test_synthetic_invalid_to_repair_to_review_to_accepted_retains_healthy_sections():
    driver = ClosureDriver()
    original_core = core_hash(driver.report)
    outputs = []

    def writer(context):
        raw = synthetic_invalid_draft(context) if context["mode"] == "draft" else correct_draft(context)
        if context["mode"] == "repair":
            assert context["section_scope"] == ["problem"]
            failed = context["failed_sections"][0]
            assert failed["payload_kind"] == "invalid_section"
            assert failed["paragraphs"][0]["paragraph_id"] == "problem-main"
            assert failed["paragraphs"][0]["text_fa"] == paragraph(outputs[0])["text_fa"]
            assert ID_PATTERN in json.dumps(context["repair_issues"], ensure_ascii=False)
            paragraph(raw)["paragraph_id"] = "problem_main"
        outputs.append(deepcopy(raw))
        return raw

    driver.writer = writer
    result = driver.run()
    assert result["mode"] == "llm" and result["review_status"] == "passed"
    assert [s["role"] for s in driver.sent] == ["explainer", "report_reviewer", "explainer", "report_reviewer"]
    assert driver.used == {"explainer": 2, "report_reviewer": 2}
    repair = driver.sent[2]["context"]
    assert "problem" not in driver.sent[1]["context"]["section_scope"]
    assert driver.sent[3]["context"]["section_scope"] == ["problem"]
    accepted = {s["section"]["section_id"]: s["section"] for s in result["accepted_sections"]}
    assert all(section_hash(s) == section_hash(accepted[s["section_id"]]) for s in repair["accepted_sections"])
    assert accepted["problem"]["paragraphs"][0]["paragraph_id"] == "problem_main"
    assert paragraph(outputs[0])["paragraph_id"] == "problem-main"
    assert driver.journal["calls"]["draft"]["result"]["output"] == outputs[0]
    assert all(s["context"]["evidence"] == driver.sent[0]["context"]["evidence"] for s in driver.sent)
    assert core_hash(driver.report) == original_core
    assert driver.run() == result and len(driver.sent) == 4


def test_all_structurally_invalid_sections_reach_repair_before_any_mock_reviewer():
    driver = ClosureDriver()
    driver.writer = lambda ctx: (synthetic_invalid_draft(ctx, all_sections=True)
                                if ctx["mode"] == "draft" else correct_draft(ctx))
    result = driver.run()
    assert result["mode"] == "llm" and result["review_status"] == "passed"
    assert [s["role"] for s in driver.sent] == ["explainer", "explainer", "report_reviewer"]
    repair = driver.sent[1]["context"]
    assert len(repair["failed_sections"]) == 5 and repair["accepted_sections"] == []
    assert all(s["payload_kind"] == "invalid_section" for s in repair["failed_sections"])


@pytest.mark.parametrize("failure", ["identifier", "sense"])
def test_failed_second_writer_preserves_call_cap_and_reviewed_fallback(failure):
    driver = ClosureDriver()

    def writer(context):
        raw = synthetic_invalid_draft(context)
        if context["mode"] == "repair" and failure == "sense":
            paragraph(raw)["paragraph_id"] = "problem_main"
            paragraph(raw)["text_fa"] = paragraph(raw)["text_fa"].replace("{{fact:problem.sense}}", "کمینه‌سازی")
        return raw

    driver.writer = writer
    result = driver.run()
    assert result["mode"] == "mixed" and result["review_status"] == "partial"
    assert driver.used == {"explainer": 2, "report_reviewer": 1} and len(driver.sent) == 3
    origins = {s["section"]["section_id"]: s["origin"] for s in result["accepted_sections"]}
    assert origins["problem"] == "server" and sum(v == "llm" for v in origins.values()) == 4
    expected = "SCHEMA_INVALID" if failure == "identifier" else "MISSING_REQUIRED_FACT"
    assert any(i["code"] == expected and i["section_id"] == "problem" for i in result["issues"])
    assert driver.run() == result and len(driver.sent) == 3


def test_repaired_id_cannot_reuse_an_accepted_paragraph_identifier():
    _, _, _, repair = prepared_repair()
    corrected = correct_draft(as_dict(repair))
    paragraph(corrected)["paragraph_id"] = repair.accepted_sections[0].paragraphs[0].paragraph_id
    result = validate_draft(repair, corrected)
    assert not result.valid_sections
    assert any(i.code == "SCHEMA_INVALID" and i.section_id == "problem" for i in result.issues)


@pytest.mark.parametrize("fault", ["scope", "old_hash"])
def test_structural_repair_cannot_return_accepted_sections_or_old_request_hash(fault):
    original, _, _, repair = prepared_repair()
    corrected = correct_draft(as_dict(repair))
    if fault == "scope":
        corrected["sections"].append(as_dict(repair.accepted_sections[0]))
    else:
        corrected["request_hash"] = original.request_hash
    result = validate_draft(repair, corrected)
    assert result.global_error and not result.valid_sections
    assert any(i.code == ("OUT_OF_SCOPE_REPAIR" if fault == "scope" else "REQUEST_MISMATCH") for i in result.issues)


def test_interrupted_structural_repair_reuses_exact_snapshot_hash_and_invocation():
    driver = ClosureDriver()

    def writer(context):
        if context["mode"] == "draft":
            return synthetic_invalid_draft(context)
        driver.crash_after_send = True
        return correct_draft(context)

    driver.writer = writer
    with pytest.raises(RuntimeError, match="process loss"):
        driver.run()
    frozen = deepcopy(driver.sent[2])
    assert frozen["context"]["failed_sections"][0]["payload_kind"] == "invalid_section"
    driver.writer = lambda context: pytest.fail("Received repair must be replayed, not regenerated")
    result = driver.run()
    assert result["mode"] == "llm" and len(driver.sent) == 4
    assert driver.sent[2] == frozen and driver.used == {"explainer": 2, "report_reviewer": 2}
    assert request_hash(frozen["context"]) == frozen["context"]["request_hash"]


@pytest.mark.parametrize("field", ["text", "instruction"])
def test_repair_snapshot_and_structural_guidance_are_bound_by_request_hash(field):
    _, _, _, repair = prepared_repair()
    altered = as_dict(repair)
    failed = altered["failed_sections"][0]
    if field == "text":
        failed["paragraphs"][0]["text_fa"] += " متن مصنوعی افزوده‌شده"
    else:
        failed["structural_errors"][0]["repair_instruction_fa"] += " دستور مصنوعی افزوده‌شده"
    assert request_hash(altered) != repair.request_hash
    result = validate_draft(altered, correct_draft(as_dict(repair)))
    assert result.global_error and any(i.code == "REQUEST_MISMATCH" for i in result.issues)


@pytest.mark.parametrize("version", ["1.0", "1.1"])
@pytest.mark.parametrize("mode", ["draft", "repair"])
def test_legacy_writer_context_keeps_existing_shape_and_request_hash(version, mode):
    context = writer_context(version)
    raw = as_dict(context)
    if mode == "repair":
        good = correct_draft(raw)["sections"]
        raw.update(mode="repair", section_scope=["problem"],
                   failed_sections=[good[0]], accepted_sections=good[1:])
        raw["request_hash"] = request_hash(raw)
    original = deepcopy(raw)
    restored = WriterContextV3.model_validate_json(json.dumps(raw, ensure_ascii=False))
    assert as_dict(restored) == original
    assert request_hash(restored) == original["request_hash"]
    assert set(original) == {"schema_version", "mode", "prompt_bundle_sha256", "evidence",
                             "context_hash", "request_hash", "section_scope", "accepted_sections",
                             "failed_sections", "previous_response_text", "previous_response_truncated", "repair_issues"}
    assert not validate_draft(restored, correct_draft(as_dict(restored))).issues


def schema_contains_pattern(value):
    if isinstance(value, dict):
        return "pattern" in value or any(schema_contains_pattern(v) for v in value.values())
    if isinstance(value, list):
        return any(schema_contains_pattern(v) for v in value)
    return False


@pytest.mark.parametrize("mode", ["draft", "repair"])
@pytest.mark.parametrize("response_mode", ["json_schema", "json_object", "text_json"])
def test_real_prepared_request_teaches_identifier_contract_when_provider_pattern_is_stripped(mode, response_mode):
    context = writer_context() if mode == "draft" else prepared_repair()[3]
    profile = snapshot().model_copy(update={"response_mode": response_mode})
    direct = build_request(profile, "explainer.v3", as_dict(context), report_generation_policy_version="1.1")
    prepared = prepare_provider_request(profile, "explainer.v3", context, report_generation_policy_version="1.1")
    assert prepared.request == direct and json.loads(prepared.body_bytes) == direct
    assert prepared.response_schema == provider_schema(NarrativeDraftV3)
    assert not schema_contains_pattern(prepared.response_schema)
    assert ID_PATTERN in prepared.system_message and "problem_main" in prepared.system_message
    assert "paragraph_id" in prepared.system_message
    transmitted = json.loads(direct["messages"][1]["content"])
    assert transmitted == as_dict(context) and request_hash(transmitted) == transmitted["request_hash"]
    if mode == "repair":
        assert transmitted["failed_sections"][0]["paragraphs"][0]["paragraph_id"] == "problem-main"
        assert transmitted["failed_sections"][0]["structural_errors"]
    assert prepared.provenance["request_body_sha256"] == sha256(prepared.body_bytes).hexdigest()
    assert prepared.provenance["effective_max_output_tokens"] == 8192
