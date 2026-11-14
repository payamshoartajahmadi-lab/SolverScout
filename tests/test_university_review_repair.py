"""Contract/replay tests, not evidence of real-model semantic-review quality.

Recovered hash defects below are real historical payloads. Controlled writers
and reviewers prove safe diagnostics, binding, bounded repair and replay only.
"""
from copy import deepcopy
import json
from pathlib import Path

import httpx
from pydantic import ValidationError
import pytest

from p_mc_optimizer.llm.narrative_models import NarrativeReviewV1, ReviewerContextV1, ReviewerContextV1_1
from p_mc_optimizer.llm.narrative_policy import freeze_narrative_policy
from p_mc_optimizer.llm.models import provider_schema
from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3, core_hash, evidence_hash, value_hash
from p_mc_optimizer.reporting.narrative_validation import NarrativeValidationError, reviewer_repair_snapshot, validate_review
from p_mc_optimizer.workflow.narrative_v3 import build_reviewer_context
from test_narrative_closure_workflow import ClosureDriver
from test_narrative_v3_workflow import BUNDLE, correct_draft, fixture, review_response

RECOVERED = json.loads((Path(__file__).parent / "fixtures/university_delivery/recovered_review_hashes.json").read_text())


@pytest.fixture(autouse=True)
def no_external_or_numerical_execution(monkeypatch):
    original_http = httpx.HTTPTransport.handle_request
    def forbidden(*args, **kwargs):
        pytest.fail("Reviewer contract tests must not send real HTTP or execute a solver/evaluator")
    def local_only(transport, request):
        if request.url.host in {'127.0.0.1', 'localhost', '::1'}:
            return original_http(transport, request)
        return forbidden()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", local_only)
    monkeypatch.setattr("p_mc_optimizer.execution.evaluator.BudgetedEvaluator.__call__", forbidden)
    monkeypatch.setattr("p_mc_optimizer.execution.supervisor.run_execution", forbidden)


def context():
    evidence = build_evidence_v3(fixture(), policy_version="1.1")
    from p_mc_optimizer.workflow.narrative_v3 import build_writer_context
    writer = build_writer_context(evidence, prompt_bundle_sha256=BUNDLE)
    draft = correct_draft(writer.model_dump(mode="json"))
    return build_reviewer_context(evidence, draft["sections"], prompt_bundle_sha256=BUNDLE,
                                  context_version="1.1")


@pytest.mark.parametrize("index", [0, 1])
def test_recovered_historical_defect_replays_under_unchanged_response_contract(index):
    case = RECOVERED["cases"][index]
    raw = deepcopy(case["response"])
    original = deepcopy(raw)
    with pytest.raises(ValidationError) as error:
        NarrativeReviewV1.model_validate(raw)
    details = error.value.errors(include_input=False, include_context=False, include_url=False)
    assert [(e["loc"], e["type"]) for e in details] == [(("request_hash",), "string_pattern_mismatch")]
    assert len(raw["request_hash"]) == case["expected_error"]["actual_characters"]
    assert len(RECOVERED["expected_request_hash"]) == 64
    corrected = deepcopy(raw)
    corrected["request_hash"] = RECOVERED["expected_request_hash"]
    assert NarrativeReviewV1.model_validate(corrected).request_hash == RECOVERED["expected_request_hash"]
    assert raw == original  # Positive structural copy does not rewrite history.


@pytest.mark.parametrize("bad_length", [54, 47])
def test_hash_diagnostic_is_safe_specific_and_bound_to_invalid_response(bad_length):
    ctx = context()
    raw = review_response(ctx.model_dump(mode="json"))
    raw["request_hash"] = raw["request_hash"][:bad_length]
    raw["Authorization"] = "Bearer SYNTHETIC_PRIVATE_REVIEW_SECRET"
    with pytest.raises(NarrativeValidationError) as error:
        validate_review(ctx, raw)
    diagnostics = [d.model_dump(mode="json") for d in error.value.diagnostics]
    target = next(d for d in diagnostics if d["field_path"] == "request_hash")
    assert target["stage"] == "structural_validation" and target["error_type"] == "string_pattern_mismatch"
    assert "64" in target["expected_rule"] and "۶۴" in target["repair_instruction_fa"]
    assert target["response_sha256"] == value_hash(raw)
    assert target["review_contract_version"] == "narrative_review/1.0"
    public = json.dumps(diagnostics, ensure_ascii=False)
    assert "SYNTHETIC_PRIVATE_REVIEW_SECRET" not in public
    assert '"input"' not in public and '"ctx"' not in public and '"msg"' not in public
    assert "<extra_field>" in public


def test_review_repair_changes_request_binding_without_changing_draft_or_evidence():
    driver = ClosureDriver()
    before = core_hash(driver.report)
    def reviewer(ctx):
        response = review_response(ctx)
        if ctx["review_repair"] is None:
            response["request_hash"] = response["request_hash"][:54]
        return response
    driver.reviewer = reviewer
    result = driver.run(version="1.2")
    assert result["mode"] == "llm" and driver.used == {"explainer": 1, "report_reviewer": 2}
    first, second = driver.sent[1:]
    assert first["context"]["schema_version"] == second["context"]["schema_version"] == "1.1"
    assert first["context"]["request_hash"] != second["context"]["request_hash"]
    assert first["invocation_id"] != second["invocation_id"]
    for field in ("draft_hash", "context_hash", "evidence", "draft_sections", "rendered_sections", "accepted_section_hashes", "section_scope"):
        assert first["context"][field] == second["context"][field]
    repair = second["context"]["review_repair"]
    assert repair["previous_invocation_id"] == first["invocation_id"]
    assert len(json.loads(repair["previous_response_text"])["request_hash"]) == 54
    assert any(d["field_path"] == "request_hash" for d in repair["validation_diagnostics"])
    assert result["attempts"][1]["validation_diagnostics"] == repair["validation_diagnostics"]
    assert core_hash(driver.report) == before
    original_first = deepcopy(driver.journal["calls"]["review"])
    assert driver.run(version="1.2") == result and len(driver.sent) == 3
    assert driver.journal["calls"]["review"] == original_first


@pytest.mark.parametrize("second", ["invalid", "semantic_reject"])
def test_exhausted_reviewer_share_never_starts_an_unreviewable_writer(second):
    driver = ClosureDriver()
    def reviewer(ctx):
        response = review_response(ctx, reject=["candidate_rationale"] if second == "semantic_reject" and ctx["review_repair"] else [])
        if ctx["review_repair"] is None or second == "invalid":
            response["request_hash"] = response["request_hash"][:47]
        return response
    driver.reviewer = reviewer
    result = driver.run(version="1.2")
    assert driver.used == {"explainer": 1, "report_reviewer": 2} and len(driver.sent) == 3
    assert result["mode"] == ("deterministic" if second == "invalid" else "mixed")
    if second == "semantic_reject":
        accepted = {x["section"]["section_id"]: x for x in result["accepted_sections"]}
        assert accepted["candidate_rationale"]["origin"] == "server"
        assert all(x["origin"] == "llm" for sid, x in accepted.items() if sid != "candidate_rationale")


def test_semantic_rejection_keeps_scoped_writer_repair_in_four_calls():
    driver = ClosureDriver()
    driver.reviewer = lambda ctx: review_response(ctx, reject=["selection"] if not ctx["accepted_section_hashes"] else [])
    result = driver.run(version="1.2")
    assert result["mode"] == "llm" and len(driver.sent) == 4
    assert driver.used == {"explainer": 2, "report_reviewer": 2}
    accepted = {s["section"]["section_id"]: s["section"] for s in result["accepted_sections"]}
    repair_context = driver.sent[2]["context"]
    assert repair_context["section_scope"] == ["selection"]
    assert all(value_hash(s) == value_hash(accepted[s["section_id"]]) for s in repair_context["accepted_sections"])
    assert driver.sent[3]["context"]["review_repair"] is None


def test_missing_transport_response_is_not_structural_feedback():
    driver = ClosureDriver()
    def reviewer(ctx):
        if driver.used["report_reviewer"] == 1:
            return {"status": "outcome_unknown", "output": None, "previous_output": None,
                    "error": {"error_code": "LLM_OUTCOME_UNKNOWN"}, "call_id": "synthetic-unknown"}
        return review_response(ctx)
    driver.reviewer = reviewer
    result = driver.run(version="1.2")
    assert result["mode"] == "llm" and len(driver.sent) == 3
    assert driver.sent[1]["context"] == driver.sent[2]["context"]
    assert driver.sent[2]["context"]["review_repair"] is None
    assert result["attempts"][1]["status"] == "unknown"


@pytest.mark.parametrize("kind,expected", [("accept_with_issue", "accept_with_issues"), ("missing_section", "section_scope_mismatch"), ("unknown_paragraph", "unknown_paragraph")])
def test_relational_review_errors_have_distinct_safe_rules(kind, expected):
    ctx = context();raw = review_response(ctx.model_dump(mode="json"), reject=["problem"])
    review = raw["section_reviews"][0]
    if kind == "accept_with_issue": review["verdict"] = "accept"
    elif kind == "missing_section": raw["section_reviews"].pop()
    else: review["issues"][0]["paragraph_id"] = "foreign_paragraph"
    with pytest.raises(NarrativeValidationError) as error: validate_review(ctx, raw)
    assert any(d.stage == "relational_validation" and d.error_type == expected for d in error.value.diagnostics)


def test_malformed_json_and_bounded_feedback_projection_are_distinct():
    ctx = context()
    with pytest.raises(NarrativeValidationError) as error: validate_review(ctx, '{"request_hash":')
    assert error.value.diagnostics[0].stage == "parsing"
    raw = review_response(ctx.model_dump(mode="json"))
    raw["unknown_private"] = "SYNTHETIC_UNKNOWN_SECRET"
    raw["section_reviews"][0]["issues"] = [{"message_fa": "api_key=SYNTHETIC_INLINE_KEY " + "x" * 20000}]
    text, truncated = reviewer_repair_snapshot(raw)
    assert truncated and len(text) <= 8192
    assert "SYNTHETIC_UNKNOWN_SECRET" not in text and "SYNTHETIC_INLINE_KEY" not in text


def test_versioned_context_preserves_legacy_closed_input_and_legacy_policy():
    modern = context()
    assert ReviewerContextV1_1.model_validate(modern.model_dump(mode="json"))
    with pytest.raises(ValidationError): ReviewerContextV1.model_validate(modern.model_dump(mode="json"))
    assert freeze_narrative_policy("1.2")["evidence_policy_version"] == "1.1"
    assert freeze_narrative_policy("1.2")["roles"]["narrative_review.v1"]["prompt_name"] == "narrative_review.v1.2"
    assert "reviewer_context_version" not in freeze_narrative_policy("1.1")
    schema = json.loads((Path(__file__).parents[1] / "schemas/narrative_review_context.v1.1.json").read_text())
    assert schema == provider_schema(ReviewerContextV1_1)


def test_real_gateway_wire_uses_versioned_feedback_and_replays_without_resend(tmp_path):
    """Real loopback API + MockTransport; no real provider/model request."""
    from hashlib import sha256
    from uuid import uuid4
    from p_mc_optimizer.llm.client import invoke_role
    from p_mc_optimizer.llm.provider import prompt_bundle_manifest
    from p_mc_optimizer.reporting.evidence_v3 import canonical_bytes
    from p_mc_optimizer.workflow.narrative_v3 import NarrativeOrchestrator
    from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, completion, golden
    from test_llm_workflow import configure, local_api, submit
    sent, seen = [], []
    def provider(request):
        body = json.loads(request.content)
        if body['messages'][1]['content'] == 'Return the connection check as JSON.':
            return completion({'ok': True})
        ctx = json.loads(body['messages'][1]['content'])
        sent.append(deepcopy(ctx))
        if 'mode' in ctx:
            assert body['max_completion_tokens'] == 8192
            return completion(correct_draft(ctx))
        assert body['max_completion_tokens'] == 4096
        assert ctx['schema_version'] == '1.1'
        assert 'Review-response repair protocol' in body['messages'][0]['content']
        response = review_response(ctx)
        if ctx['review_repair'] is None: response['request_hash'] = response['request_hash'][:54]
        return completion(response)
    with local_api(tmp_path, httpx.MockTransport(provider)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, golden()['original_text'])
        worker = str(uuid4()); repo.claim_job(worker); repo.update_job(job_id, phase='reporting', worker_id=worker)
        contracts = repo.get_job(job_id)['workflow']['execution_contracts']
        assert contracts['report_generation_policy_version'] == '1.2'
        report = json.loads(json.dumps(fixture()).replace(fixture()['job_id'], job_id))
        initial = core_hash(report)
        def invoke(role, schema, ctx, invocation):
            seen.append((role, schema, deepcopy(ctx), invocation))
            return invoke_role(address, token, job_id, role, ctx, invocation_id=invocation, schema_id=schema)
        result = NarrativeOrchestrator(report, invoke=invoke,
            persist=lambda value: repo.put_workflow(job_id, 'narrative_v3_journal', value, worker_id=worker),
            prompt_bundle_sha256=sha256(canonical_bytes(prompt_bundle_manifest())).hexdigest(),
            report_generation_policy_version='1.2').run()
        assert result['mode'] == 'llm' and len(sent) == 3
        assert sent[1]['request_hash'] != sent[2]['request_hash']
        assert sent[1]['draft_hash'] == sent[2]['draft_hash'] and sent[1]['evidence'] == sent[2]['evidence']
        role, schema, ctx, invocation = seen[-1]
        replay = invoke_role(address, token, job_id, role, ctx, invocation_id=invocation, schema_id=schema)
        assert replay['status'] == 'succeeded' and len(sent) == 3
        assert core_hash(report) == initial
        with repo.connect() as db:
            rows = db.execute('SELECT output,metadata FROM llm_calls WHERE job_id=? AND role=? ORDER BY rowid', (job_id, 'report_reviewer')).fetchall()
        assert len(rows) == 2 and len(json.loads(rows[0]['output'])['request_hash']) == 54
        assert all(json.loads(row['metadata'])['prompt_name'] == 'narrative_review.v1.2' for row in rows)
