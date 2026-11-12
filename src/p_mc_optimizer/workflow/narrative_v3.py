"""Bounded, durable orchestration for grounded narrative reporting.

The only external operation is the injected single-attempt gateway primitive.
This module cannot run an evaluator, solver, research transport, or model code.
"""
from __future__ import annotations

from copy import deepcopy
from time import monotonic
from uuid import uuid4

from p_mc_optimizer.llm.narrative_models import (
    SECTION_ORDER, REQUIRED_SECTIONS, NarrativeSectionV3, ReportEvidenceV3,
    ReviewerContextV1, WriterContextV3,
)
from p_mc_optimizer.reporting.evidence_v3 import (
    build_evidence_v3, canonical_bytes, core_hash, evidence_hash, value_hash,
)
from p_mc_optimizer.reporting.narrative_validation import (
    NarrativeValidationError, draft_hash, render_sections, request_hash,
    validate_draft, validate_review,
)
from p_mc_optimizer.llm.narrative_policy import freeze_narrative_policy


JOURNAL_KEY = "narrative_v3_journal"
_PROVIDER_ERRORS = {
    "PROVIDER_CONFIGURATION_REQUIRED", "PROVIDER_AUTH_FAILED", "PROVIDER_TIMEOUT",
    "PROVIDER_UNREACHABLE", "PROVIDER_RATE_LIMITED", "PROVIDER_SERVER_ERROR",
    "PROVIDER_STRUCTURED_OUTPUT_UNSUPPORTED", "LLM_OUTCOME_UNKNOWN",
    "UNEXPECTED_PROVIDER_RESPONSE", "UNSUPPORTED_PROVIDER_CONFIGURATION",
}


def _json(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else deepcopy(value)


def _ordered(sections):
    by_id = {item["section_id"]: item for item in map(_json, sections)}
    return [by_id[key] for key in SECTION_ORDER if key in by_id]


def _issue(code, *, section=None, message=None):
    return {
        "section_id": section, "paragraph_id": None, "code": code, "evidence_refs": [],
        "message_fa": message or "متن گزارش با شواهد یا قرارداد این درخواست سازگار نیست.",
        "required_change_fa": "بخش مشخص‌شده را فقط با شواهد همین اجرا اصلاح کنید.",
    }


def build_writer_context(evidence, *, prompt_bundle_sha256, scope=SECTION_ORDER,
                         accepted_sections=(), failed_sections=None,
                         previous_response_text=None, repair_issues=()):
    raw = {
        "schema_version": "3.0", "mode": "repair" if failed_sections is not None else "draft",
        "prompt_bundle_sha256": prompt_bundle_sha256, "evidence": _json(evidence),
        "context_hash": evidence_hash(evidence), "request_hash": "0" * 64,
        "section_scope": list(scope), "accepted_sections": _ordered(accepted_sections),
        "failed_sections": _ordered(failed_sections or []),
        "previous_response_text": previous_response_text[:8192] if previous_response_text is not None else None,
        "previous_response_truncated": previous_response_text is not None and len(previous_response_text) > 8192,
        "repair_issues": list(map(_json, repair_issues)),
    }
    raw["request_hash"] = request_hash(raw)
    return WriterContextV3.model_validate(raw)


def build_reviewer_context(evidence, sections, *, prompt_bundle_sha256, scope=None,
                           accepted_sections=()):
    assembled = _ordered(list(accepted_sections) + list(sections))
    context_digest = evidence_hash(evidence)
    scope = [item["section_id"] for item in map(_json, sections)] if scope is None else scope
    raw = {
        "schema_version": "1.0", "prompt_bundle_sha256": prompt_bundle_sha256,
        "evidence": _json(evidence), "context_hash": context_digest, "request_hash": "0" * 64,
        "draft_hash": draft_hash(context_digest, assembled), "draft_sections": assembled,
        "rendered_sections": list(map(_json, render_sections(evidence, assembled))),
        "section_scope": [sid for sid in SECTION_ORDER if sid in scope],
        "accepted_section_hashes": [{"section_id": item["section_id"], "sha256": value_hash(item)}
                                    for item in _ordered(accepted_sections)],
    }
    raw["request_hash"] = request_hash(raw)
    return ReviewerContextV1.model_validate(raw)


class NarrativeOrchestrator:
    """Run at most two writer and two reviewer attempts within the job ledger.

    ``persist`` must durably replace the journal before returning. ``invoke``
    must call the existing gateway once, with the supplied invocation ID.
    ``budget`` includes all roles already charged to this job, including unknown
    outcomes, not merely this narrative's own attempts.
    """

    def __init__(self, report, *, invoke, persist, journal=None, budget=None,
                 current_report=None, cancelled=lambda: False, shutdown=lambda: False,
                 prompt_bundle_sha256, report_generation_policy_version=None):
        self.report = deepcopy(report)
        self.invoke, self.persist = invoke, persist
        self.journal = deepcopy(journal or {})
        self.budget = budget or (lambda: {"total_used": 0, "by_role": {}, "max_total": 8, "max_per_role": 2})
        self.current_report = current_report or (lambda: deepcopy(self.report))
        self.cancelled, self.shutdown = cancelled, shutdown
        self.prompt_bundle_sha256 = prompt_bundle_sha256
        # A durable journal wins over a new caller's default. It is never upgraded.
        journal_version = self.journal.get("report_generation_policy_version", "1.0")
        self.report_generation_policy_version = (journal_version if self.journal else
                                                 report_generation_policy_version or "1.0")
        self.generation_policy = freeze_narrative_policy(self.report_generation_policy_version)
        self.policy_mismatch = bool(self.journal and (
            report_generation_policy_version not in (None, journal_version)
            or self.journal.get("report_generation_policy", self.generation_policy if journal_version == "1.0" else None) != self.generation_policy
            or self.journal.get("report_generation_policy_hash", self.generation_policy["report_generation_policy_hash"] if journal_version == "1.0" else None) != self.generation_policy["report_generation_policy_hash"]
            or self.journal.get("prompt_bundle_sha256", prompt_bundle_sha256) != prompt_bundle_sha256
            or any(call.get("context", {}).get("prompt_bundle_sha256", prompt_bundle_sha256) != prompt_bundle_sha256
                   for call in self.journal.get("calls", {}).values())
        ))
        self.started = monotonic()
        self.issues = []
        self.accepted = {}
        self.omitted = set()
        self.provider_failure = False
        self.interrupted = False
        self.review_ran = False
        self.evidence = None

    def _save(self):
        self.journal["issues"] = deepcopy(self.issues)
        self.journal["accepted_sections"] = list(self.accepted.values())
        self.journal["accepted_section_hashes"] = [
            {"section_id": key, "sha256": value_hash(item["section"])}
            for key, item in self.accepted.items()
        ]
        self.journal["omitted_sections"] = [key for key in SECTION_ORDER if key in self.omitted]
        self.persist(deepcopy(self.journal))

    def _add_issues(self, issues):
        for issue in map(_json, issues):
            if issue not in self.issues:
                self.issues.append(issue)

    def _mark_invalid(self, key, issues):
        call = self.journal.get("calls", {}).get(key)
        if call and call.get("attempt") and issues:
            if call["attempt"]["status"] not in {"unknown", "cancelled"}:
                call["attempt"]["status"] = "failed"
            call["attempt"]["error_code"] = call["attempt"]["error_code"] or _json(issues[0])["code"]
            self._save()

    def _room(self, *, writer=0, reviewer=0, total=0):
        state = self.budget()
        policy = self.report.get("policy") or {}
        limit = min(8 if policy.get("policy_version") in {"2.0", "2.1"} else 6,
                    state.get("max_total", policy.get("max_llm_calls", 8)))
        per_role = min(2, state.get("max_per_role", policy.get("max_llm_calls_per_role", 2)))
        used = state.get("by_role") or {}
        # Local records also guard a caller-supplied budget view that is stale.
        own = self.journal.get("calls", {})
        own_writer = sum(item["role"] == "explainer" for item in own.values())
        own_review = sum(item["role"] == "report_reviewer" for item in own.values())
        return (max(state.get("total_used", 0), len(own)) + total <= limit
                and max(used.get("explainer", 0), own_writer) + writer <= per_role
                and max(used.get("report_reviewer", 0), own_review) + reviewer <= per_role)

    def _contexts_fit(self, context):
        if len(canonical_bytes(context)) <= 196608:
            return True
        self._add_issues([_issue("CONTEXT_TOO_LARGE", message="زمینهٔ گزارش از سقف مجاز بزرگ‌تر است؛ شواهد بریده نشدند.")])
        return False

    def _current_policy_matches(self, report):
        contracts = report.get("execution_contracts") or {}
        marker = report.get("report_generation_policy_version", contracts.get("report_generation_policy_version"))
        if marker is not None and marker != self.report_generation_policy_version:
            return False
        if contracts and contracts.get("explanation") == "grounded_narrative_v3":
            version = contracts.get("report_generation_policy_version", "1.0")
            return (version == self.report_generation_policy_version
                    and contracts.get("evidence_builder_version", "1.0") == version
                    and contracts.get("formatter_version", "1.0") == version
                    and contracts.get("report_generation_policy", self.generation_policy if version == "1.0" else None) == self.generation_policy)
        return True

    def _call(self, key, role, schema_id, operation, context, *, draft_digest=None):
        calls = self.journal.setdefault("calls", {})
        prior = calls.get(key)
        if prior is not None:
            if prior["context"] != context or prior["request_hash"] != context["request_hash"]:
                self._add_issues([_issue("REQUEST_MISMATCH")])
                return None
            if "result" in prior:
                return deepcopy(prior["result"])
        if self.cancelled() or self.shutdown():
            self.interrupted = True
            return None
        try:
            current = self.current_report()
            if self.journal.get("source_core_sha256") != core_hash(current) or not self._current_policy_matches(current):
                self._add_issues([_issue("CONTEXT_MISMATCH")])
                return None
        except (ValueError, TypeError):
            self._add_issues([_issue("CONTEXT_MISMATCH")])
            return None
        if not self._contexts_fit(context):
            return None
        if prior is None:
            needed = {"writer": 1, "reviewer": 1, "total": 2} if role == "explainer" else {"reviewer": 1, "total": 1}
            if not self._room(**needed):
                self._add_issues([_issue("SCHEMA_INVALID", message="سهم باقی‌مانده برای تولید و بازبینی متن کافی نیست.")])
                return None
            prior = {
                "role": role, "schema_id": schema_id, "operation": operation,
                "context": deepcopy(context), "request_hash": context["request_hash"],
                "draft_hash": draft_digest, "invocation_id": str(uuid4()),
            }
            calls[key] = prior
        # This exact active request is the gateway's reservation authorization.
        self.journal["active_request"] = {
            "status": "ready", "invocation_id": prior["invocation_id"],
            "role": role, "schema_id": schema_id, "operation": operation,
            "context": deepcopy(context), "request_hash": context["request_hash"],
        }
        self._save()
        result = self.invoke(role, schema_id, deepcopy(context), prior["invocation_id"])
        prior["result"] = deepcopy(result)
        error = result.get("error") or {}
        status = result.get("status")
        prior["attempt"] = {
            "role": role, "schema_id": schema_id, "operation": operation,
            "invocation_id": prior["invocation_id"], "call_id": result.get("call_id"),
            "request_hash": context["request_hash"], "draft_hash": draft_digest,
            "status": "succeeded" if status == "succeeded" else "unknown" if status in {"unknown", "outcome_unknown", "reserved"} else "cancelled" if status == "cancelled" else "failed",
            "error_code": error.get("error_code"),
        }
        self.journal["active_request"]["status"] = "received"
        self._save()
        return deepcopy(result)

    def _writer_context(self, scope, *, failed=None, previous=None, repair_issues=None):
        return build_writer_context(
            self.evidence, prompt_bundle_sha256=self.prompt_bundle_sha256, scope=scope,
            accepted_sections=[item["section"] for item in self.accepted.values()],
            failed_sections=failed, previous_response_text=previous,
            repair_issues=repair_issues or [],
        )

    def _review(self, sections, scope, key):
        if not scope:
            return [], []
        assembled = _ordered([item["section"] for item in self.accepted.values()] + list(sections))
        context = build_reviewer_context(
            self.evidence, sections, prompt_bundle_sha256=self.prompt_bundle_sha256,
            scope=scope, accepted_sections=[item["section"] for item in self.accepted.values()],
        )
        raw = _json(context)
        # Retry only the identical reviewer request, and only within its role cap.
        for retry in range(2):
            result = self._call(key + (":retry" if retry else ""), "report_reviewer", "narrative_review.v1",
                                "review_retry" if retry else "review", _json(context), draft_digest=raw["draft_hash"])
            if result is None:
                return [], []
            self.review_ran = True
            if result.get("status") != "succeeded" and result.get("output") is None:
                # A missing response is not a semantic/schema verdict. Preserve
                # the actual transport/unknown error and retry this context once.
                continue
            try:
                review = validate_review(context, result.get("output"))
            except (NarrativeValidationError, ValueError, TypeError) as exc:
                problems = getattr(exc, "issues", [_issue("SCHEMA_INVALID", message="پاسخ بازبین معتبر نبود.")])
                self._add_issues(problems)
                self._mark_invalid(key + (":retry" if retry else ""), problems)
                continue
            by_id = {item["section_id"]: item for item in assembled}
            rejected, issues = [], []
            call = self.journal["calls"][key + (":retry" if retry else "")]
            for verdict in review.section_reviews:
                if verdict.verdict == "accept":
                    self.accepted[verdict.section_id] = {
                        "section": deepcopy(by_id[verdict.section_id]), "origin": "llm",
                        "source_draft_hash": raw["draft_hash"],
                        "reviewer_invocation_id": call["invocation_id"],
                    }
                else:
                    rejected.append(by_id[verdict.section_id])
                    issues.extend(verdict.issues)
            self._add_issues(issues)
            self._save()
            return rejected, list(map(_json, issues))
        return [], []

    def _finish(self):
        from p_mc_optimizer.reporting.narrative_v3 import finalize_narrative
        current = self.current_report()
        invalid = self.evidence is None
        try:
            changed = self.evidence is not None and (core_hash(current) != self.journal.get("source_core_sha256") or not self._current_policy_matches(current))
        except (ValueError, TypeError):
            changed = True
        if changed or self.cancelled():
            self.accepted.clear()
            self.omitted.add("mathematical_analysis")
            self._add_issues([_issue("CONTEXT_MISMATCH", message="داده یا وضعیت اجرا پس از تولید متن تغییر کرد؛ متن قبلی منتشر نشد.")])
            try:
                self.evidence = build_evidence_v3(current, policy_version=self.report_generation_policy_version)
                invalid = False
            except (ValueError, TypeError):
                self.evidence = None
                invalid = True
        attempts = [item["attempt"] for item in self.journal.get("calls", {}).values() if "attempt" in item]
        result = finalize_narrative(
            current, self.evidence, list(self.accepted.values()), issues=self.issues,
            attempts=attempts, omitted_sections=[key for key in SECTION_ORDER if key in self.omitted],
            elapsed_reporting_seconds=monotonic() - self.started,
            provider_failure=self.provider_failure,
            requested=bool(current.get("policy", {}).get("explain_via_llm") and current.get("job_status") == "completed" and not self.cancelled()),
            evidence_invalid=invalid, review_ran=self.review_ran,
            report_generation_policy_version=self.report_generation_policy_version,
        )
        if not self.shutdown():
            self.journal["result"] = deepcopy(result)
        self.journal["active_request"] = None
        self._save()
        return result

    def run(self):
        try:
            if self.policy_mismatch:
                self._add_issues([_issue("CONTEXT_MISMATCH", message="سیاست یا prompt فریز‌شدهٔ journal تغییر کرده است؛ ادامهٔ تماس مجاز نیست.")])
                self.evidence = build_evidence_v3(self.report, policy_version=self.report_generation_policy_version)
                return self._finish()
            initial_core = core_hash(self.report)
            if self.journal.get("source_core_sha256") not in (None, initial_core):
                self._add_issues([_issue("CONTEXT_MISMATCH")])
                self.evidence = build_evidence_v3(self.report, policy_version=self.report_generation_policy_version)
                return self._finish()
            if self.journal.get("result") is not None:
                self.evidence = ReportEvidenceV3.model_validate(self.journal["evidence"])
                if self.evidence.evidence_policy_version != self.report_generation_policy_version:
                    raise ValueError("Cached evidence policy mismatch")
                if core_hash(self.current_report()) == initial_core and not self.cancelled():
                    return deepcopy(self.journal["result"])
                return self._finish()
            self.evidence = ReportEvidenceV3.model_validate(self.journal["evidence"]) if self.journal.get("evidence") else build_evidence_v3(self.report, policy_version=self.report_generation_policy_version)
            if self.evidence.evidence_policy_version != self.report_generation_policy_version:
                raise ValueError("Evidence policy does not match the frozen journal")
        except (ValueError, TypeError):
            self._add_issues([_issue("SCHEMA_INVALID", message="شواهد عددی گزارش معتبر نیستند؛ متن مدل تولید نشد.")])
            return self._finish()
        self.journal.update({"schema_version": "3.0", "context_hash": evidence_hash(self.evidence),
                             "source_core_sha256": initial_core, "evidence": _json(self.evidence),
                             "report_generation_policy_version": self.report_generation_policy_version,
                             "report_generation_policy": deepcopy(self.generation_policy),
                             "report_generation_policy_hash": self.generation_policy["report_generation_policy_hash"],
                             "prompt_bundle_sha256": self.prompt_bundle_sha256})
        self.journal.setdefault("calls", {})
        self._save()
        eligible = self.report.get("job_status") == "completed" and self.report.get("policy", {}).get("explain_via_llm")
        if not eligible or self.cancelled() or self.shutdown():
            return self._finish()
        if self.invoke is None:
            self.provider_failure = True
            context = _json(self._writer_context(SECTION_ORDER))
            self.journal["calls"]["configuration"] = {
                "role": "explainer", "schema_id": "explainer.v3", "context": context,
                "request_hash": context["request_hash"],
                "attempt": {"role": "explainer", "schema_id": "explainer.v3", "operation": "draft",
                            "invocation_id": str(uuid4()), "call_id": None,
                            "request_hash": context["request_hash"], "draft_hash": None,
                            "status": "failed", "error_code": "PROVIDER_CONFIGURATION_REQUIRED"},
            }
            return self._finish()
        context = self._writer_context(SECTION_ORDER)
        result = self._call("draft", "explainer", "explainer.v3", "draft", _json(context))
        if result is None:
            return self._finish()
        raw = result.get("output")
        error_code = (result.get("error") or {}).get("error_code")
        if result.get("status") != "succeeded" and error_code not in {"LLM_SCHEMA_INVALID", "LLM_OUTPUT_TRUNCATED"}:
            self.provider_failure = error_code in _PROVIDER_ERRORS or result.get("status") in {"unknown", "outcome_unknown"}
            return self._finish()
        checked = validate_draft(context, raw)
        self._add_issues(checked.issues)
        self._mark_invalid("draft", checked.issues)
        self.omitted.update(checked.omitted_sections)
        valid = list(map(_json, checked.valid_sections))
        structural_failed = []
        if isinstance(raw, dict):
            valid_ids = {item["section_id"] for item in valid}
            for section in raw.get("sections", []):
                try:
                    item = _json(NarrativeSectionV3.model_validate(section))
                except ValueError:
                    continue
                if item["section_id"] not in valid_ids:
                    structural_failed.append(item)
        rejected, review_issues = [], []
        if valid:
            rejected, review_issues = self._review(valid, [item["section_id"] for item in valid], "review")
        unresolved = [key for key in REQUIRED_SECTIONS if key not in self.accepted]
        if checked.global_error:
            unresolved = list(SECTION_ORDER)
        optional_failed = any(issue["section_id"] == "mathematical_analysis" for issue in self.issues)
        if optional_failed and "mathematical_analysis" not in self.accepted:
            unresolved.append("mathematical_analysis")
        if not unresolved or self.cancelled() or self.shutdown():
            return self._finish()
        # A failed reviewer may have used its remaining slot. Never repair text
        # that cannot then receive its mandatory independent review.
        if "repair" not in self.journal["calls"] and not self._room(writer=1, reviewer=1, total=2):
            return self._finish()
        failed = _ordered(structural_failed + rejected)
        previous = result.get("previous_output")
        if checked.global_error and previous is None:
            previous = canonical_bytes(raw).decode("utf-8") if raw is not None else "null"
        repair_context = self._writer_context(unresolved, failed=failed, previous=previous if checked.global_error else None,
                                              repair_issues=checked.issues + review_issues)
        repaired = self._call("repair", "explainer", "explainer.v3", "repair", _json(repair_context))
        if repaired is None:
            return self._finish()
        error_code = (repaired.get("error") or {}).get("error_code")
        if repaired.get("status") != "succeeded" and error_code not in {"LLM_SCHEMA_INVALID", "LLM_OUTPUT_TRUNCATED"}:
            self.provider_failure = error_code in _PROVIDER_ERRORS
            return self._finish()
        fixed = validate_draft(repair_context, repaired.get("output"))
        self._add_issues(fixed.issues)
        self._mark_invalid("repair", fixed.issues)
        self.omitted.update(fixed.omitted_sections)
        valid_repair = list(map(_json, fixed.valid_sections))
        self.omitted.difference_update(item["section_id"] for item in valid_repair)
        if valid_repair:
            self._review(valid_repair, [item["section_id"] for item in valid_repair], "repair_review")
        return self._finish()
