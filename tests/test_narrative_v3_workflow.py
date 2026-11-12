"""Bounded reporting state transitions with a controlled semantic reviewer."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from p_mc_optimizer.reporting.evidence_v3 import core_hash, value_hash
from p_mc_optimizer.workflow.narrative_v3 import NarrativeOrchestrator


FIXTURE = Path(__file__).parent / "fixtures/narrative_v3/F1.json"
BUNDLE = "a" * 64


def fixture():
    report = json.loads(FIXTURE.read_text())
    report["policy"]["explain_via_llm"] = True
    return report


def correct_draft(context, *, variant=False, math_section=False):
    requirements = {item["section_id"]: item for item in context["evidence"]["sections"]}
    sections = []
    for sid in context["section_scope"]:
        if sid == "mathematical_analysis" and not math_section:
            if context["mode"] == "repair":
                sections.append({"section_id": sid, "paragraphs": []})
            continue
        req = requirements[sid]
        refs = req["required_fact_refs"]
        if sid == "mathematical_analysis":
            refs = ["problem.expression", "constant.zero"]
        words = "این بخش داده‌های ثبت‌شده را به ترتیب توضیح می‌دهد: " if variant else "شرح این آزمایش و شواهد مربوط به آن: "
        sections.append({"section_id": sid, "paragraphs": [{
            "paragraph_id": "p_" + sid, "claim_kind": "interpretation" if sid == "mathematical_analysis" else "recorded",
            "text_fa": words + "؛ ".join("{{fact:" + ref + "}}" for ref in refs),
            "fact_refs": refs, "evidence_refs": req["allowed_evidence_refs"],
        }]})
    return {"schema_version": "3.0", "context_hash": context["context_hash"],
            "request_hash": context["request_hash"], "sections": sections}


def review_response(context, reject=()):
    return {"schema_version": "1.0", "context_hash": context["context_hash"],
            "request_hash": context["request_hash"], "draft_hash": context["draft_hash"],
            "section_reviews": [{"section_id": sid, "verdict": "reject" if sid in reject else "accept",
                                 "issues": [{"section_id": sid, "paragraph_id": None,
                                             "code": "WRONG_SELECTION_REASON", "evidence_refs": ["report:decision"],
                                             "message_fa": "علت با تصمیم همین اجرا سازگار نیست.",
                                             "required_change_fa": "دلیل ثبت‌شدهٔ همین تصمیم را توضیح دهید."}] if sid in reject else []}
                                for sid in context["section_scope"]]}


class Driver:
    def __init__(self, report=None, *, total=8):
        self.report = report or fixture()
        self.journal = {}
        self.sent = []
        self.results = {}
        self.used = {"explainer": 0, "report_reviewer": 0}
        self.total = total
        self.writer = lambda ctx: correct_draft(ctx)
        self.reviewer = lambda ctx: review_response(ctx)
        self.shutdown = False
        self.cancelled = False
        self.crash_after_send = False

    def save(self, journal):
        # JSON round-trip is intentional: restart state cannot retain Python objects.
        self.journal = json.loads(json.dumps(journal, ensure_ascii=False, allow_nan=False))

    def invoke(self, role, schema, context, invocation):
        active = self.journal["active_request"]
        assert active["status"] == "ready" and active["invocation_id"] == invocation
        assert active["role"] == role and active["schema_id"] == schema
        assert active["context"] == context and active["request_hash"] == context["request_hash"]
        if invocation in self.results:
            return deepcopy(self.results[invocation])
        self.sent.append({"role": role, "schema": schema, "context": deepcopy(context), "invocation_id": invocation})
        self.used[role] += 1
        value = (self.writer if role == "explainer" else self.reviewer)(context)
        if isinstance(value, dict) and "status" in value:
            result = value
        else:
            result = {"status": "succeeded", "call_id": invocation, "output": value,
                      "previous_output": None, "error": None}
        self.results[invocation] = result
        if self.crash_after_send:
            self.crash_after_send = False
            raise RuntimeError("Simulated process loss after remote result, before local processing")
        return deepcopy(result)

    def run(self):
        engine = NarrativeOrchestrator(
            self.report, invoke=self.invoke, persist=self.save, journal=self.journal,
            budget=lambda: {"total_used": sum(self.used.values()), "by_role": self.used,
                            "max_total": self.total, "max_per_role": 2},
            current_report=lambda: self.report, cancelled=lambda: self.cancelled,
            shutdown=lambda: self.shutdown, prompt_bundle_sha256=BUNDLE,
        )
        return engine.run()


def test_free_prose_is_not_a_note_catalog_and_core_never_changes():
    """N3-12/26: two different narratives, pure reporting, stable numerical core."""
    results = []
    for variant in (False, True):
        driver = Driver()
        driver.writer = lambda ctx: correct_draft(ctx, variant=variant)
        before = core_hash(driver.report)
        result = driver.run()
        assert result["mode"] == "llm" and result["review_status"] == "passed"
        assert len(driver.sent) == 2 and driver.used == {"explainer": 1, "report_reviewer": 1}
        assert before == core_hash(driver.report)
        results.append(result["rendered_sections"])
    assert results[0] != results[1]


def test_one_semantically_rejected_section_is_repaired_without_rewriting_accepted_text():
    """N3-13/17: reviewer verdict controls repair scope and immutable accepted hashes."""
    driver = Driver()
    driver.reviewer = lambda ctx: review_response(ctx, reject=["selection"] if not ctx["accepted_section_hashes"] else [])
    result = driver.run()
    assert result["mode"] == "llm" and len(driver.sent) == 4
    repair = driver.sent[2]["context"]
    assert repair["mode"] == "repair" and repair["section_scope"] == ["selection"]
    assert len(repair["accepted_sections"]) == 4
    after = {item["section"]["section_id"]: item["section"] for item in result["accepted_sections"]}
    assert all(after[item["section_id"]] == item for item in repair["accepted_sections"])
    assert driver.sent[3]["context"]["section_scope"] == ["selection"]
    assert all(value_hash(after[item["section_id"]]) == item["sha256"] for item in driver.sent[3]["context"]["accepted_section_hashes"])


def test_local_invalid_binding_does_not_discard_structurally_valid_sections():
    """N3-09/17: the first review sees only valid sections; repair sees the invalid one."""
    driver = Driver()
    def write(context):
        value = correct_draft(context)
        if context["mode"] == "draft":
            value["sections"][0]["paragraphs"][0]["text_fa"] += " {{fact:foreign_job.value}}"
            value["sections"][0]["paragraphs"][0]["fact_refs"].append("foreign_job.value")
        return value
    driver.writer = write
    result = driver.run()
    assert result["mode"] == "llm"
    assert "problem" not in driver.sent[1]["context"]["section_scope"]
    assert driver.sent[2]["context"]["section_scope"] == ["problem"]
    assert any(issue["code"] == "UNKNOWN_FACT" for issue in result["issues"])


def test_optional_math_removal_needs_no_reviewer_call_for_the_removal():
    """N3-16/19: only rejected optional math may be removed by an empty repair."""
    driver = Driver()
    driver.writer = lambda ctx: correct_draft(ctx, math_section=ctx["mode"] == "draft")
    driver.reviewer = lambda ctx: review_response(ctx, reject=["mathematical_analysis"])
    result = driver.run()
    assert result["mode"] == "llm" and len(driver.sent) == 3
    assert result["omitted_sections"] == ["mathematical_analysis"]
    assert len(result["accepted_sections"]) == 5


def test_malformed_json_full_repair_has_its_own_bounded_previous_response():
    """N3-20: malformed response differs from transport failure; one full repair only."""
    driver = Driver()
    def writer(ctx):
        if ctx["mode"] == "draft":
            return {"status": "failed", "call_id": "failed-json", "output": None,
                    "previous_output": "{" + "x" * 10000,
                    "error": {"error_code": "LLM_SCHEMA_INVALID"}}
        assert len(ctx["previous_response_text"]) == 8192 and ctx["previous_response_truncated"]
        assert len(ctx["section_scope"]) == 6
        return correct_draft(ctx)
    driver.writer = writer
    result = driver.run()
    assert result["mode"] == "llm" and len(driver.sent) == 3
    assert driver.used == {"explainer": 2, "report_reviewer": 1}
    assert driver.journal["calls"]["draft"]["result"]["previous_output"].endswith("x" * 10000)


@pytest.mark.parametrize("code", ["PROVIDER_TIMEOUT", "PROVIDER_UNREACHABLE", "LLM_OUTCOME_UNKNOWN"])
def test_writer_provider_failure_has_no_automatic_retry(code):
    driver = Driver()
    driver.writer = lambda _: {"status": "outcome_unknown" if code == "LLM_OUTCOME_UNKNOWN" else "failed",
                               "call_id": "unknown", "output": None, "previous_output": None,
                               "error": {"error_code": code}}
    result = driver.run()
    assert result["mode"] == "deterministic" and len(driver.sent) == 1
    assert result["review_status"] == "not_run"


def test_invalid_reviewer_retries_identical_request_only_once_and_publishes_no_model_text():
    driver = Driver()
    driver.reviewer = lambda _: {}
    result = driver.run()
    assert result["mode"] == "deterministic" and len(driver.sent) == 3
    assert driver.sent[1]["context"] == driver.sent[2]["context"]
    assert driver.used == {"explainer": 1, "report_reviewer": 2}
    assert all(item["origin"] == "server" for item in result["accepted_sections"])


@pytest.mark.parametrize("limit,expected_calls,expected_mode", [(1, 0, "deterministic"), (2, 2, "mixed"), (3, 2, "mixed"), (4, 4, "llm")])
def test_budget_reserves_both_writing_and_reviewing_capacity(limit, expected_calls, expected_mode):
    driver = Driver(total=limit)
    driver.reviewer = lambda ctx: review_response(ctx, reject=["selection"] if not ctx["accepted_section_hashes"] else [])
    result = driver.run()
    assert len(driver.sent) == expected_calls and result["mode"] == expected_mode
    assert sum(driver.used.values()) <= limit


def test_crash_after_send_reuses_invocation_and_does_not_double_charge():
    driver = Driver()
    driver.crash_after_send = True
    with pytest.raises(RuntimeError, match="process loss"):
        driver.run()
    invocation = driver.sent[0]["invocation_id"]
    result = driver.run()
    assert result["mode"] == "llm" and len(driver.sent) == 2
    assert driver.sent[0]["invocation_id"] == invocation
    assert driver.used == {"explainer": 1, "report_reviewer": 1}
    assert driver.run() == result and len(driver.sent) == 2


def test_crash_during_repair_replays_the_same_attempt_and_accepted_sections():
    driver = Driver()
    driver.reviewer = lambda ctx: review_response(ctx, reject=["selection"] if not ctx["accepted_section_hashes"] else [])
    def writer(ctx):
        if ctx["mode"] == "repair":
            driver.crash_after_send = True
        return correct_draft(ctx)
    driver.writer = writer
    with pytest.raises(RuntimeError, match="process loss"):
        driver.run()
    assert len(driver.sent) == 3
    original_id = driver.sent[2]["invocation_id"]
    driver.writer = lambda ctx: correct_draft(ctx)
    result = driver.run()
    assert result["mode"] == "llm" and len(driver.sent) == 4
    assert driver.sent[2]["invocation_id"] == original_id
    assert driver.used == {"explainer": 2, "report_reviewer": 2}


@pytest.mark.parametrize("mutate", ["cancel", "core"])
def test_late_writer_output_cannot_be_attached_to_changed_or_cancelled_run(mutate):
    driver = Driver()
    def writer(context):
        output = correct_draft(context)
        if mutate == "cancel":
            driver.cancelled = True
            driver.report["job_status"] = "cancelled"
        else:
            driver.report["best_x"] = [0.0, 0.0]
        return output
    driver.writer = writer
    result = driver.run()
    assert result["mode"] == "deterministic"
    assert len(driver.sent) == 1
    assert all(item["origin"] == "server" for item in result["accepted_sections"])
    assert any(issue["code"] == "CONTEXT_MISMATCH" for issue in result["issues"])


def test_disabled_explanation_never_calls_writer_or_reviewer():
    driver = Driver()
    driver.report["policy"]["explain_via_llm"] = False
    before = core_hash(driver.report)
    result = driver.run()
    assert not driver.sent and result["mode"] == "deterministic"
    assert before == core_hash(driver.report)


@pytest.mark.parametrize("fault", ["scope", "old_hash"])
def test_bad_repair_cannot_touch_accepted_sections_or_reuse_a_stale_request(fault):
    """N3-18: server acceptance survives an invalid second writer response."""
    driver = Driver()
    initial_hash = []
    def write(ctx):
        value = correct_draft(ctx)
        if ctx["mode"] == "draft":
            initial_hash.append(ctx["request_hash"])
        elif fault == "scope":
            value["sections"].append(deepcopy(ctx["accepted_sections"][0]))
        else:
            value["request_hash"] = initial_hash[0]
        return value
    driver.writer = write
    driver.reviewer = lambda ctx: review_response(ctx, reject=["selection"])
    result = driver.run()
    assert result["mode"] == "mixed" and len(driver.sent) == 3
    assert len([item for item in result["accepted_sections"] if item["origin"] == "llm"]) == 4
    assert any(issue["code"] == ("OUT_OF_SCOPE_REPAIR" if fault == "scope" else "REQUEST_MISMATCH") for issue in result["issues"])


def test_all_local_sections_invalid_go_directly_to_repair_without_a_useless_review():
    driver = Driver()
    def write(ctx):
        value = correct_draft(ctx)
        if ctx["mode"] == "draft":
            for section in value["sections"]:
                section["paragraphs"][0]["text_fa"] += " 999"
        return value
    driver.writer = write
    result = driver.run()
    assert result["mode"] == "llm"
    assert [item["role"] for item in driver.sent] == ["explainer", "explainer", "report_reviewer"]


def test_existing_preparatory_role_charges_leave_no_hidden_reporting_capacity():
    driver = Driver()
    driver.used["formulator"] = 2
    driver.used["advisor"] = 2
    driver.used["research_planner"] = 2
    driver.total = 7
    result = driver.run()
    assert result["mode"] == "deterministic" and not driver.sent


def test_shutdown_defers_new_calls_but_restart_reuses_the_durable_received_draft():
    driver = Driver()
    def write(ctx):
        value = correct_draft(ctx)
        driver.shutdown = True
        return value
    driver.writer = write
    stopped = driver.run()
    assert stopped["mode"] == "deterministic" and len(driver.sent) == 1
    assert "result" not in driver.journal
    driver.shutdown = False
    result = driver.run()
    assert result["mode"] == "llm" and len(driver.sent) == 2


def test_oversized_repair_envelope_keeps_frozen_evidence_and_makes_no_extra_http_call():
    driver = Driver()
    before = core_hash(driver.report)
    def large(context):
        value = correct_draft(context, math_section=True)
        for section in value["sections"]:
            paragraph = section["paragraphs"][0]
            paragraph["text_fa"] += "🧮" * (2200 - len(paragraph["text_fa"]))
            section["paragraphs"] = [{**deepcopy(paragraph), "paragraph_id": paragraph["paragraph_id"] + "_" + str(i)} for i in range(3)]
        return value
    driver.writer = large
    result = driver.run()
    assert result["mode"] == "deterministic" and len(driver.sent) == 1
    assert any(issue["code"] == "CONTEXT_TOO_LARGE" for issue in result["issues"])
    assert core_hash(driver.report) == before


def test_reviewer_transfer_retry_preserves_the_actual_provider_error_and_request():
    driver = Driver()
    def review(ctx):
        if driver.used["report_reviewer"] == 1:
            return {"status": "failed", "call_id": "review-timeout", "output": None,
                    "error": {"error_code": "PROVIDER_TIMEOUT"}}
        return review_response(ctx)
    driver.reviewer = review
    result = driver.run()
    assert result["mode"] == "llm" and len(driver.sent) == 3
    assert result["attempts"][1]["error_code"] == "PROVIDER_TIMEOUT"
    assert driver.sent[1]["context"] == driver.sent[2]["context"]


def test_provider_failure_during_repair_keeps_the_reviewed_healthy_sections():
    driver = Driver()
    driver.reviewer = lambda ctx: review_response(ctx, reject=["selection"])
    def write(ctx):
        if ctx["mode"] == "repair":
            return {"status": "failed", "call_id": "repair-timeout", "output": None,
                    "error": {"error_code": "PROVIDER_TIMEOUT"}}
        return correct_draft(ctx)
    driver.writer = write
    result = driver.run()
    assert result["mode"] == "mixed" and len(driver.sent) == 3
    assert result["review_status"] == "partial"
    assert result["attempts"][-1]["error_code"] == "PROVIDER_TIMEOUT"
    assert sum(item["origin"] == "llm" for item in result["accepted_sections"]) == 4


@pytest.mark.parametrize("behavior", ["success", "repair", "fallback"])
def test_every_reporting_path_export_and_reopen_has_zero_execution_calls(monkeypatch, behavior):
    """N3-26: counters sit at execution boundaries, not merely the budget display."""
    from p_mc_optimizer.execution import evaluator, supervisor
    from p_mc_optimizer.solvers import registry
    from p_mc_optimizer.catalog import adapter
    from p_mc_optimizer.reporting import html_bytes
    from p_mc_optimizer.reporting.narrative_v3 import apply_narrative_v3
    from p_mc_optimizer.reporting.presentation import build_report_view_model
    calls = []
    def forbidden(*args, **kwargs):
        calls.append("execution")
        pytest.fail("Reporting reached the objective/solver execution boundary")
    monkeypatch.setattr(evaluator.BudgetedEvaluator, "__call__", forbidden)
    monkeypatch.setattr(supervisor, "run_execution", forbidden)
    monkeypatch.setattr(registry, "run_solver", forbidden)
    monkeypatch.setattr(registry, "run_mealpy", forbidden)
    monkeypatch.setattr(adapter, "run_catalog_solver", forbidden)
    driver = Driver()
    if behavior == "repair":
        driver.reviewer = lambda ctx: review_response(ctx, reject=["selection"] if not ctx["accepted_section_hashes"] else [])
    elif behavior == "fallback":
        driver.writer = lambda _: {"status": "failed", "call_id": "provider", "output": None,
                                   "error": {"error_code": "PROVIDER_UNREACHABLE"}}
    before = core_hash(driver.report)
    output = driver.run()
    report = apply_narrative_v3(driver.report, output)
    assert core_hash(report) == before
    json_export = json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))
    assert build_report_view_model(json_export)
    assert html_bytes(json_export)
    assert driver.run() == output  # completed reporting replay/reopen is not regeneration
    assert core_hash(json_export) == before and calls == []


@pytest.mark.parametrize("section,bad_text,code", [
    ("selection", "با وجود نبود تضمین عمومی، علت انتخاب تساوی میانه‌ها و پراکندگی کمتر بود.", "WRONG_SELECTION_REASON"),
    ("verification_and_limits", "با وجود نبود تضمین کلی، برنامه در همین اجرا گواه رسمی بهینگی صادر کرد.", "FALSE_CERTIFICATION"),
    ("candidate_rationale", "وجود این منبع ثابت می‌کند روش منتخب همیشه در همهٔ مسائل برتر است.", "SOURCE_NOT_SUPPORTING"),
    ("solution", "مصرف اجرای اصلی، همان مصرف کل کار است و این دو کمیت یکسان‌اند.", "WRONG_BUDGET_SCOPE"),
])
def test_semantic_rejection_of_false_claim_survives_valid_bindings_and_disclaimer(section, bad_text, code):
    """N3-13/14/15: controlled review tests software, not real LLM comprehension."""
    driver = Driver()
    def writer(ctx):
        draft = correct_draft(ctx)
        if ctx["mode"] == "draft":
            next(item for item in draft["sections"] if item["section_id"] == section)["paragraphs"][0]["text_fa"] += " " + bad_text
            draft["sections"][0]["paragraphs"][0]["text_fa"] += " بهینگی سراسری در این اجرا اثبات نشده است و موفقیت عمومی تضمین نمی‌شود."
        return draft
    def reviewer(ctx):
        rendered = {item["section_id"]: item for item in ctx["rendered_sections"]}
        if not ctx["accepted_section_hashes"]:
            assert bad_text in rendered[section]["paragraphs"][0]["text_fa"]
            assert "تضمین نمی‌شود" in rendered["problem"]["paragraphs"][0]["text_fa"]
            review = review_response(ctx, reject=[section])
            next(item for item in review["section_reviews"] if item["section_id"] == section)["issues"][0]["code"] = code
            return review
        assert bad_text not in rendered[section]["paragraphs"][0]["text_fa"]
        return review_response(ctx)
    driver.writer, driver.reviewer = writer, reviewer
    result = driver.run()
    assert result["mode"] == "llm" and len(driver.sent) == 4
    assert driver.sent[2]["context"]["section_scope"] == [section]
    assert any(issue["code"] == code for issue in result["issues"])
    problem = next(item for item in result["rendered_sections"] if item["section_id"] == "problem")
    assert "تضمین نمی‌شود" in problem["paragraphs"][0]["text_fa"]
