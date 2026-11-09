"""Durable LangGraph workflow. SQLite, rather than a checkpoint, owns side effects."""

from __future__ import annotations

import hashlib
import platform
import tempfile
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from time import monotonic
from typing import Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from p_mc_optimizer.domain.models import ExecutionPolicy, JobPhase, ProblemInput, ProblemRevision
from p_mc_optimizer.execution.supervisor import run_execution
from p_mc_optimizer.llm.client import REPAIRABLE_ERROR_CODES, invoke_role
from p_mc_optimizer.llm.storage import job_call_metadata
from p_mc_optimizer.problems.registry import freeze_problem
from p_mc_optimizer.reporting import (
    TRIAL_COLUMNS, add_progress, csv_bytes, evaluation_records, html_bytes, json_bytes, template_explanation,
)
from p_mc_optimizer.selection.policies import (
    SelectionContext, SelectionPolicy, derive_seed, validate_plan,
)
from p_mc_optimizer.solvers.registry import solver_cards, trial_request

LLM_POLICIES = {"llm_only", "llm_shortlist_then_pilot"}
FATAL_NUMERIC = {"OBJECTIVE_EVALUATION_FAILED", "DETERMINISM_CHECK_FAILED", "RECOVERY_REQUIRED"}
SELECTION = SelectionPolicy()


class GraphState(TypedDict, total=False):
    job_id: str
    phase: JobPhase
    route: Literal["next", "report", "halt"]


def _model(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _code(error):
    return (error or {}).get("error_code") or (error or {}).get("code")


def _error(code, message):
    return {"error_code": code, "message_fa": message}


def _seconds_since(timestamp):
    if not timestamp:
        return None
    return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds())


class Workflow:
    def __init__(self, repo, job_id, worker_id, *, api_url=None, token=None, shutdown=lambda: False):
        self.repo = repo
        self.job_id = job_id
        self.worker_id = worker_id
        self.api_url = api_url
        self.token = token
        self.shutdown = shutdown
        self.report = None
        self._cancel_check = 0.0
        self._cancel_requested = False

    @property
    def job(self):
        return self.repo.get_job(self.job_id)

    @property
    def workflow(self):
        return self.job["workflow"]

    @property
    def policy(self):
        return ExecutionPolicy.model_validate(self.job["policy"])

    @property
    def selection_policy(self):
        return self.policy.selection_policy

    @property
    def revision(self):
        raw = self.repo.get_problem(self.job_id)["revision"]
        return ProblemRevision.model_validate_json(json_bytes(raw, pretty=False)) if raw else None

    def keep(self, key, value):
        self.repo.put_workflow(self.job_id, key, _model(value), worker_id=self.worker_id)

    def phase(self, phase):
        self.repo.update_job(self.job_id, worker_id=self.worker_id, phase=phase,
                             event_type="phase_changed", event_payload={"phase": phase})

    def event(self, event_type, **payload):
        self.repo.append_event(self.job_id, event_type, payload, worker_id=self.worker_id)

    def cancelled(self, *, immediate=False):
        if self._cancel_requested:
            return True
        now = monotonic()
        if immediate or now >= self._cancel_check:
            self._cancel_requested = bool(self.job["cancel_requested"])
            self._cancel_check = now + 0.1
        return self._cancel_requested

    def route_cancel(self):
        self.keep("stop", {"status": "cancelled", "reason": "cancelled", "error": None})
        return {"route": "report"}

    def stop(self, code, message, *, reason="execution_error", status="failed"):
        self.keep("stop", {"status": status, "reason": reason, "error": _error(code, message)})
        return {"route": "report"}

    def needs_configuration(self, error):
        code = _code(error) or "PROVIDER_CONFIGURATION_REQUIRED"
        self.repo.update_job(self.job_id, worker_id=self.worker_id, job_status="needs_configuration",
                             blocked_reason=code, error=error, event_type="provider_blocked",
                             event_payload={"error_code": code})
        return {"route": "halt"}

    def intake(self):
        self.phase("intake")

    def _invoke(self, role, context, *, validate=lambda output: output):
        if self.api_url is None:
            return None, _error("PROVIDER_CONFIGURATION_REQUIRED", "تنظیمات مدل در این اجرا در دسترس نیست.")
        digest = hashlib.sha256(json_bytes(context, pretty=False)).hexdigest()
        journal_key = f"llm:{role}:{self.workflow.get('resume_count', 0)}:{digest}"
        journal = dict(self.workflow.get(journal_key, {}))
        for index in range(2):
            if self.cancelled(immediate=True) or self.shutdown():
                return None, _error("CANCELLED", "درخواست توقف ثبت شد.")
            invocation_id = journal.get(str(index))
            if invocation_id is None:
                invocation_id = str(uuid4())
                journal[str(index)] = invocation_id
                self.keep(journal_key, journal)
            timeout = (self.job["llm_profile"] or {}).get("request_timeout_seconds", 60)
            result = invoke_role(
                self.api_url, self.token, self.job_id, role, context,
                invocation_id=invocation_id, request_timeout_seconds=timeout,
            )
            if result["call_id"] is None:
                return None, result["error"]
            prior_output = result.get("output")
            previous_output = result.get("previous_output")
            if result.get("status") == "succeeded":
                try:
                    return validate(prior_output), None
                except (ValueError, TypeError, KeyError) as exc:
                    error = _error("LLM_SCHEMA_INVALID", "خروجی مدل با شواهد یا قرارداد مسئله سازگار نیست.")
                    error["field_errors"] = [str(exc).splitlines()[0][:180]]
            else:
                error = result.get("error") or _error("LLM_SCHEMA_INVALID", "خروجی مدل قابل استفاده نیست.")
            if _code(error) not in REPAIRABLE_ERROR_CODES or index == 1:
                return None, error
            repair = {"previous_output": (json_bytes(prior_output, pretty=False).decode()[:3000]
                                          if prior_output is not None else previous_output),
                      "field_errors": [str(item)[:180] for item in (error.get("field_errors") or [])]}
            context = {**context, "repair": repair}
        raise AssertionError("unreachable")

    def formulate_problem(self):
        from p_mc_optimizer.llm.formulation import convert_formulation
        from p_mc_optimizer.llm.models import FormulatorResponse

        self.phase("formulating")
        job = self.job
        if job["input"]["kind"] != "natural_language" or self.revision or job.get("completion_input"):
            self.event("llm_skipped", role="formulator", reason="structured_input")
            return
        if self.workflow.get("formulated_problem"):
            return
        draft = self.repo.get_problem(self.job_id)["draft"]
        questions = {a["question_id"]: a["question_fa"] for a in (draft or {}).get("ambiguities", [])}
        sources = {"input:text": job["input"]["text"]}
        for answer in job.get("answers", []):
            qid = answer["question_id"]
            sources[f"answer:{qid}"] = answer["answer"]
        context = {"sources": [
            {"source_ref": ref, "text": value, **({"question_fa": questions[ref[7:]]}
              if ref.startswith("answer:") and ref[7:] in questions else {})}
            for ref, value in sources.items()
        ]}
        parsed, error = self._invoke(
            "formulator", context,
            validate=lambda output: convert_formulation(FormulatorResponse.model_validate(output), sources),
        )
        if error:
            if self.cancelled(immediate=True):
                return self.route_cancel()
            code = _code(error)
            if code in REPAIRABLE_ERROR_CODES:
                return self.stop("LLM_SCHEMA_INVALID", "صورت‌بندی معتبر دریافت نشد.", reason="validation_error")
            if code == "LLM_BUDGET_EXHAUSTED" and draft:
                self.repo.update_job(self.job_id, worker_id=self.worker_id, job_status="needs_input",
                                     blocked_reason=code, error=error, event_type="needs_input")
                return {"route": "halt"}
            return self.needs_configuration(error)
        parsed = _model(parsed)
        draft_version = (draft or {}).get("draft_version", 0) + 1
        persisted = {
            "draft_id": (draft or {}).get("draft_id") or str(uuid4()),
            "draft_version": draft_version, "original_text": job["input"]["text"],
            "input_artifact_refs": [],
            **{k: parsed[k] for k in ("extracted_fields", "missing_fields", "ambiguities",
                                     "source_map", "normalizations", "unsupported_features") if k in parsed},
        }
        self.repo.put_draft(self.job_id, persisted, worker_id=self.worker_id)
        if parsed.get("unsupported_features"):
            return self.stop("UNSUPPORTED_PROBLEM", "مسئله خارج از دامنهٔ نسخهٔ اول است.",
                             reason="validation_error")
        if parsed.get("problem") is None or parsed.get("ambiguities"):
            self.repo.update_job(self.job_id, worker_id=self.worker_id, job_status="needs_input",
                                 blocked_reason="AMBIGUOUS_PROBLEM", event_type="needs_input",
                                 event_payload={"draft_version": draft_version})
            return {"route": "halt"}
        self.keep("formulated_problem", parsed["problem"])

    def validate_problem(self):
        self.phase("validating_input")
        if self.revision is not None:
            return
        job = self.job
        data = job.get("completion_input") or job["input"].get("problem") or self.workflow.get("formulated_problem")
        try:
            ProblemInput.model_validate(data)
        except ValueError:
            return self.stop("INVALID_SCHEMA", "مدل مسئله کامل یا معتبر نیست.", reason="validation_error")

    def freeze_problem(self):
        if self.revision is not None:
            return
        job = self.job
        data = job.get("completion_input") or job["input"].get("problem") or self.workflow.get("formulated_problem")
        draft = self.repo.get_problem(self.job_id)["draft"]
        source_map = (draft or {}).get("source_map") if self.workflow.get("formulated_problem") and not job.get("completion_input") else None
        try:
            revision = freeze_problem(ProblemInput.model_validate(data), source_map=source_map)
        except ValueError as exc:
            code = getattr(exc, "code", "INVALID_EXPRESSION")
            return self.stop(code, "تابع یا قرارداد ارزیابی مسئله معتبر نیست.", reason="validation_error")
        self.repo.freeze_problem(self.job_id, _model(revision), worker_id=self.worker_id)

    def _time_left(self, phase):
        elapsed = sum(
            (row.get("outcome") or {}).get("elapsed_compute_seconds", 0)
            for row in self.repo.list_executions(self.job_id)
        )
        policy = self.policy
        if phase == "verification":
            return min(policy.verification_time_reserve_seconds, policy.max_compute_seconds - elapsed)
        return policy.max_compute_seconds - policy.verification_time_reserve_seconds - elapsed

    def _execute(self, phase, quota, *, algorithm_id=None, replicate_index=0, reference=None):
        from p_mc_optimizer.domain.models import ExecutionRequest, ExecutionResult

        revision = self.revision
        rows = self.repo.list_executions(self.job_id)
        for row in rows:
            trial = row.get("request") or {}
            if (row["phase"] == phase and trial.get("algorithm_id") == algorithm_id and
                    trial.get("replicate_index", 0) == replicate_index):
                if row["outcome"]:
                    return ExecutionResult.model_validate(row["outcome"])
                return None
        deadline = self._time_left(phase)
        if deadline <= 0 or self.cancelled(immediate=True) or self.shutdown():
            return None
        seed = derive_seed(self.policy.master_seed, revision.problem_math_hash,
                           phase, algorithm_id or "none", replicate_index)
        trial = None
        execution_id = str(uuid4())
        if algorithm_id is not None:
            trial = trial_request(
                revision, job_id=self.job_id, trial_id=str(uuid4()), attempt_id=execution_id,
                idempotency_key=str(uuid4()), algorithm_id=algorithm_id, seed=seed, phase=phase,
                replicate_index=replicate_index, evaluation_quota=quota, deadline_seconds=deadline,
            )
        row = self.repo.start_execution(self.job_id, phase, quota, deadline, trial_request=_model(trial),
                                        execution_id=execution_id, worker_id=self.worker_id)
        if not row["created"]:
            return ExecutionResult.model_validate(row["outcome"]) if row["outcome"] else None
        request = ExecutionRequest(
            job_id=self.job_id, execution_id=execution_id, phase=phase, revision=revision,
            seed=seed, evaluation_quota=quota, deadline_seconds=deadline, trial=trial,
            verification_reference=reference,
        )

        def event(value):
            if value["kind"] == "started":
                return self.repo.record_started(execution_id, sequence=value["sequence"], worker_id=self.worker_id)

        with tempfile.TemporaryDirectory(prefix=".execution-", dir=self.repo.data_dir / "artifacts") as work:
            history = Path(work) / "evaluations.jsonl"
            result = run_execution(request, history_path=history, on_event=event,
                                   cancelled=lambda: self.cancelled() or self.shutdown())
            values = _model(result)
            history_file = history if history.exists() else history.with_suffix(history.suffix + ".part")
            if history_file.exists():
                artifact = self.repo.save_artifact(self.job_id, "evaluation_history", history_file,
                                                   suffix=".jsonl", worker_id=self.worker_id)
                values["history_artifact_id"] = artifact["artifact_id"]
            diagnostic = history.with_name(history.name + ".traceback.txt")
            if diagnostic.is_file():
                self.repo.save_artifact(self.job_id, "numeric_diagnostic", diagnostic,
                                        suffix=".txt", worker_id=self.worker_id)
            self.repo.finish_execution(execution_id, values, worker_id=self.worker_id)
        return result

    def _search_error(self, result):
        if self.cancelled(immediate=True):
            return self.route_cancel()
        if result is None or result.termination_reason == "time_budget":
            self.keep("stop", {"status": "completed", "reason": "time_budget", "error": None})
            return {"route": "report"}
        error = _model(result.error)
        if _code(error) in FATAL_NUMERIC or result.execution_status in {"unknown_outcome", "cancelled"}:
            return self.stop(_code(error) or "RECOVERY_REQUIRED",
                             "اجرای عددی به دلیل خطای تابع یا نتیجهٔ نامعلوم متوقف شد.")
        return None

    def preflight(self):
        self.phase("preflight")
        result = self._execute("preflight", self.policy.preflight_evaluations)
        failure = self._search_error(result)
        if failure:
            return failure
        if result.execution_status != "completed":
            return self.stop(_code(_model(result.error)) or "EXECUTION_ERROR", "پیش‌آزمایش عددی کامل نشد.")

    def profile_problem(self):
        self.phase("profiling")
        result = self._execute("profile", self.policy.profile_evaluations)
        failure = self._search_error(result)
        if failure:
            return failure
        if result.execution_status != "completed":
            return self.stop(_code(_model(result.error)) or "EXECUTION_ERROR", "نمونه‌برداری عددی کامل نشد.")
        features = _model(result.profile_features)
        if features:
            row = next(r for r in self.repo.list_executions(self.job_id) if r["phase"] == "profile")
            features["sample_artifact_id"] = row["history_artifact_id"]
        self.keep("profile", features)

    def filter_solvers(self):
        self.phase("planning")
        self.keep("solver_cards", solver_cards())

    def propose_candidates(self):
        from p_mc_optimizer.domain.models import CandidatePlan

        if self.workflow.get("candidate_plan"):
            return
        policy = self.policy
        if policy.selection_policy not in LLM_POLICIES:
            self.keep("candidate_plan", SELECTION.propose(SelectionContext(policy, self.revision)))
            self.keep("proposal_source", policy.selection_policy)
            self.event("llm_skipped", role="advisor", reason="baseline")
            return
        spec = _model(self.revision.spec)
        spec.pop("source_map", None)
        cards = self.workflow["solver_cards"]
        profile = self.workflow.get("profile") or {}
        evidence_refs = sorted([f"solver:{card['algorithm_id']}" for card in cards] +
                               [f"feature:{name}" for name in profile if name != "sample_artifact_id"])
        profile.pop("sample_artifact_id", None)
        count = 1 if policy.selection_policy == "llm_only" else 3
        context = {"problem": spec, "profile": profile, "solver_cards": cards,
                   "candidate_count": count, "evidence_refs": evidence_refs}
        plan, error = self._invoke("advisor", context, validate=lambda output: validate_plan(
            CandidatePlan.model_validate(output), count=count, allowed_evidence_refs=evidence_refs))
        if error:
            if self.cancelled(immediate=True):
                return self.route_cancel()
            if _code(error) not in REPAIRABLE_ERROR_CODES:
                return self.needs_configuration(error)
            self.keep("proposal_error", error)
        source = "deterministic_fallback" if error else "llm_proposal"
        proposal = SELECTION.propose(SelectionContext(policy, self.revision, plan, source))
        self.keep("candidate_plan", proposal)
        self.keep("proposal_source", source)

    def validate_plan(self):
        from p_mc_optimizer.domain.models import CandidatePlan
        count = {"pilot_only": 4, "llm_shortlist_then_pilot": 3}.get(self.selection_policy, 1)
        validate_plan(CandidatePlan.model_validate(self.workflow["candidate_plan"]), count=count)

    def run_pilots(self):
        self.phase("piloting")
        if not self.policy.pilot_candidate_count:
            self.event("stage_skipped", stage="pilot", reason="policy")
            return
        for row in sorted(self.workflow["candidate_plan"]["candidates"], key=lambda row: row["algorithm_id"]):
            for replicate in range(self.policy.pilot_repeats):
                result = self._execute("pilot", self.policy.pilot_evaluations,
                                       algorithm_id=row["algorithm_id"], replicate_index=replicate)
                failure = self._search_error(result)
                if failure:
                    return failure

    def select_solver(self):
        from p_mc_optimizer.domain.models import CandidatePlan, ExecutionResult, TrialRequest
        self.phase("selecting")
        if self.repo.get_decision(self.job_id):
            return
        policy = self.policy
        plan = CandidatePlan.model_validate(self.workflow["candidate_plan"])
        results = [(TrialRequest.model_validate(row["request"]),
                    ExecutionResult.model_validate(row["outcome"]))
                   for row in self.repo.list_executions(self.job_id)
                   if row["phase"] == "pilot" and row["outcome"]]
        context = SelectionContext(policy, self.revision, plan, self.workflow["proposal_source"])
        decision = SELECTION.decide(results, context)
        self.repo.put_decision(self.job_id, _model(decision), worker_id=self.worker_id)

    def run_main_trial(self):
        self.phase("solving")
        decision = self.repo.get_decision(self.job_id)
        algorithm = decision.get("selected_algorithm_id")
        if algorithm is None:
            self.keep("stop", {"status": "completed", "reason": "insufficient_evidence", "error": None})
            self.event("stage_skipped", stage="main", reason="insufficient_evidence")
            return
        result = self._execute("main", self.policy.main_evaluations, algorithm_id=algorithm)
        failure = self._search_error(result)
        if failure:
            return failure
        self.keep("stop", {"status": "completed", "reason": result.termination_reason,
                           "error": _model(result.error)})

    def _best(self):
        observations = [(row["outcome"]["best"], row) for row in self.repo.list_executions(self.job_id)
                        if row["phase"] != "verification" and (row.get("outcome") or {}).get("best")]
        return min(observations, key=lambda pair: pair[0]["score_for_minimization"], default=(None, None))

    def verify_solution(self):
        from p_mc_optimizer.domain.models import EvaluationRecord

        self.phase("verifying")
        best, _ = self._best()
        stop = self.workflow.get("stop") or {}
        if not best or stop.get("status") in {"failed", "cancelled"}:
            self.event("stage_skipped", stage="verification", reason="no_verifiable_candidate")
            return
        result = self._execute("verification", self.policy.verification_evaluations,
                               reference=EvaluationRecord.model_validate(best))
        if self.cancelled(immediate=True):
            return self.route_cancel()
        if result is not None:
            self.keep("verification", _model(result.validation))
            error = _model(result.error)
            if _code(error) in FATAL_NUMERIC:
                self.stop(_code(error), "ارزیابی تابع در بررسی مجدد با خطا مواجه شد.")
            elif result.validation and result.validation.validation_status == "failed":
                stop["reason"] = "validation_error"
                self.keep("stop", stop)
            elif result.execution_status in {"failed", "timed_out", "unknown_outcome"}:
                self.keep("verification_error", error)

    def _artifact_path(self, artifact_id):
        return self.repo.artifact_path(artifact_id)

    def user_wait_seconds(self):
        wait_started = None
        waiting = 0.0
        for event in self.repo.list_events(self.job_id):
            if event["event_type"] in {"needs_input", "provider_blocked"}:
                wait_started = datetime.fromisoformat(event["timestamp"])
            elif wait_started is not None and event["event_type"] in {"answers_submitted", "job_resumed"}:
                waiting += (datetime.fromisoformat(event["timestamp"]) - wait_started).total_seconds()
                wait_started = None
        if wait_started is not None:
            waiting += (datetime.now(timezone.utc) - wait_started).total_seconds()
        return max(0.0, waiting)

    def build_report_data(self):
        self.phase("reporting")
        job = self.job
        wf = job["workflow"]
        rows = self.repo.list_executions(self.job_id)
        best, origin_row = self._best()
        decision = self.repo.get_decision(self.job_id) or {}
        verification = wf.get("verification")
        stop = wf.get("stop") or {}
        verification_status = (verification or {}).get("validation_status") or (verification or {}).get("status")
        job_status = stop.get("status", "completed")
        if self.cancelled(immediate=True):
            job_status = "cancelled"
            stop = {"status": job_status, "reason": "cancelled", "error": None}
        if not best:
            solution_status = "no_valid_solution"
        elif job_status == "cancelled":
            solution_status = "validation_incomplete"
        elif verification_status == "passed":
            solution_status = "valid_best_found"
        elif verification_status == "failed":
            solution_status = "validation_failed"
        else:
            solution_status = "validation_incomplete"
        confirmed = best if solution_status == "valid_best_found" else None
        origins = None if best is None else {
            "phase": origin_row["phase"], "execution_id": origin_row["execution_id"],
            "trial_id": origin_row["trial_id"], "algorithm_id": (origin_row.get("request") or {}).get("algorithm_id"),
            "evaluation_id": best["evaluation_id"],
        }
        trials = []
        histories = []
        for row in rows:
            outcome = row.get("outcome") or {}
            if row["trial_id"] is not None:
                trials.append({**row["request"], **outcome,
                               "best_evaluation_id": (outcome.get("best") or {}).get("evaluation_id"),
                               "best_x": (outcome.get("best") or {}).get("x"),
                               "objective_raw": (outcome.get("best") or {}).get("objective_raw"),
                               "score_for_minimization": (outcome.get("best") or {}).get("score_for_minimization"),
                               "error_code": _code(outcome.get("error"))})
            artifact_id = row["history_artifact_id"]
            records = evaluation_records(self._artifact_path(artifact_id)) if artifact_id else ()
            histories.append((records, row["known_started"], row["phase"]))
        calls = job_call_metadata(self.repo, self.job_id)
        status = "pending" if self.policy.explain_via_llm and job_status == "completed" else "not_requested"
        main_success = any(row["phase"] == "main" and row["execution_status"] == "completed" for row in rows)
        partial = (solution_status != "valid_best_found" or job_status != "completed" or bool(stop.get("error"))
                   or stop.get("reason") in {"insufficient_evidence", "time_budget"})
        revision = _model(self.revision)
        self.report = {
            "schema_version": "1.0", "job_id": self.job_id, "job_status": job_status, "phase": "reporting",
            "input": job["input"], "problem_revision": revision, "policy": job["policy"],
            "problem_math_hash": (revision or {}).get("problem_math_hash"),
            "evaluation_contract_hash": (revision or {}).get("evaluation_contract_hash"),
            "profile": wf.get("profile"), "solver_cards": wf.get("solver_cards"),
            "candidate_plan": wf.get("candidate_plan"), "decision": decision or None,
            "proposal_source": wf.get("proposal_source"), "proposal_error": wf.get("proposal_error"),
            "selection_status": decision.get("selection_status", "not_reached"),
            "selected_algorithm_id": decision.get("selected_algorithm_id"),
            "selected_config_id": decision.get("selected_config_id"),
            "solution_origin": origins, "best_evaluation_id": confirmed["evaluation_id"] if confirmed else None,
            "best_x": confirmed["x"] if confirmed else None,
            "objective_raw": confirmed["objective_raw"] if confirmed else None,
            "score_for_minimization": confirmed["score_for_minimization"] if confirmed else None,
            "verification": verification, "verification_error": wf.get("verification_error"),
            "solution_status": solution_status, "optimality_status": "not_certified",
            "termination_reason": stop.get("reason") or "solver_stop",
            "main_success": main_success, "partial_result": partial, "trials": trials,
            "progress": add_progress(histories),
            "execution_history_artifacts": [row["history_artifact_id"] for row in rows if row["history_artifact_id"]],
            "diagnostics": {"unverified_candidate": best} if best and not confirmed else None,
            "budget": job["budget"], "error": stop.get("error"), "explanation_status": status,
            "llm_profile": {key: value for key, value in (job.get("llm_profile") or {}).items()
                            if key not in {"credential_ref", "credential_generation"}} or None,
            "llm_calls": calls, "cost_estimate": None,
            "timing": {"elapsed_compute_seconds": sum((row["outcome"] or {}).get("elapsed_compute_seconds", 0)
                                                       for row in rows),
                       "elapsed_wall_seconds": _seconds_since(job.get("created_at")),
                       "elapsed_user_wait_seconds": self.user_wait_seconds(),
                       "elapsed_llm_seconds": sum(call.get("elapsed_seconds", 0) or 0 for call in calls)},
            "environment": {"python": platform.python_version(), "platform": platform.platform(),
                            "numpy": version("numpy"), "mealpy": version("mealpy"), "rng": "PCG64",
                            "registry_version": "1.0", "prompt_version": "1.0",
                            "master_seed": self.policy.master_seed},
        }

    def explain_result(self):
        report = self.report
        if self.cancelled(immediate=True) and report["job_status"] != "cancelled":
            self.build_report_data()
            report = self.report
        if report["explanation_status"] == "pending" and not self.shutdown():
            from p_mc_optimizer.llm.models import ExplainerResponse

            evidence = ["report:solution", "report:verification", "report:budget"]
            if report["selected_algorithm_id"] is not None:
                evidence.append("report:decision")
            summary = {k: report[k] for k in ("selected_algorithm_id", "selection_status", "solution_status",
                                               "solution_origin", "verification", "partial_result", "budget",
                                               "optimality_status", "objective_raw")}
            context = {"report": summary, "evidence_refs": evidence,
                       "selection_made": report["selected_algorithm_id"] is not None}
            explanation, error = self._invoke("explainer", context,
                                              validate=ExplainerResponse.model_validate)
            if self.cancelled(immediate=True):
                self.build_report_data()
                report = self.report
            elif explanation:
                report["explanation_status"] = "generated"
                report["explanation"] = _model(explanation)
            else:
                report["explanation_status"] = "schema_fallback" if _code(error) in REPAIRABLE_ERROR_CODES else "provider_error"
                report["explanation_error"] = error
        else:
            self.event("llm_skipped", role="explainer", reason=report["job_status"])
        if not report.get("explanation"):
            report["explanation"] = template_explanation(report)
        report["llm_calls"] = job_call_metadata(self.repo, self.job_id)
        report["timing"]["elapsed_wall_seconds"] = _seconds_since(self.job.get("created_at"))
        report["timing"]["elapsed_llm_seconds"] = sum(
            call.get("elapsed_seconds", 0) or 0 for call in report["llm_calls"])

    def finalize_job(self):
        if self.shutdown() and not self.cancelled(immediate=True):
            return {"route": "halt"}
        if self.cancelled(immediate=True) and self.report["job_status"] != "cancelled":
            self.build_report_data()
            self.explain_result()
        report = self.report
        artifacts = [
            self.repo.save_artifact(self.job_id, "report_json", json_bytes(report), suffix=".json", worker_id=self.worker_id),
            self.repo.save_artifact(self.job_id, "report_html", html_bytes(report), suffix=".html", worker_id=self.worker_id),
            self.repo.save_artifact(self.job_id, "trials_csv", csv_bytes(report["trials"], TRIAL_COLUMNS),
                                    suffix=".csv", worker_id=self.worker_id),
        ]
        fields = {k: report[k] for k in ("job_status", "solution_status", "optimality_status", "selection_status",
                                         "explanation_status", "termination_reason", "partial_result", "error")}
        self.repo.publish_report(self.job_id, report, [x["artifact_id"] for x in artifacts],
                                 worker_id=self.worker_id, **fields)

    def run(self):
        pipeline = [
            "intake", "formulate_problem", "validate_problem", "freeze_problem", "preflight", "profile_problem",
            "filter_solvers", "propose_candidates", "validate_plan", "run_pilots", "select_solver",
            "run_main_trial", "verify_solution", "build_report_data", "explain_result", "finalize_job",
        ]
        early_search = {"preflight", "profile_problem", "run_pilots", "run_main_trial"}

        def node(name):
            def invoke(state):
                if self.shutdown() and not self.cancelled(immediate=True):
                    return {"route": "halt"}
                if name not in {"build_report_data", "explain_result", "finalize_job"} and self.cancelled():
                    return self.route_cancel()
                try:
                    update = getattr(self, name)()
                except Exception:
                    if self.cancelled(immediate=True):
                        return self.route_cancel()
                    raise
                return {"route": "next", "phase": self.job["phase"], **(update or {})}
            return invoke

        def router(name):
            def route(state):
                outcome = state["route"]
                if outcome == "report" and name in early_search and (
                    (self.workflow.get("stop") or {}).get("reason") == "time_budget"
                ):
                    return "verify"
                return outcome
            return route

        graph = StateGraph(GraphState)
        for name in pipeline:
            graph.add_node(name, node(name))
        graph.add_edge(START, pipeline[0])
        for name, following in zip(pipeline, [*pipeline[1:], END]):
            graph.add_conditional_edges(name, router(name), {
                "next": following, "report": "build_report_data", "verify": "verify_solution", "halt": END,
            })
        with SqliteSaver.from_conn_string(str(self.repo.data_dir / "graph_checkpoints.sqlite3")) as saver:
            compiled = graph.compile(checkpointer=saver)
            compiled.invoke({"job_id": self.job_id, "route": "next"},
                            config={"configurable": {"thread_id": self.job_id}, "recursion_limit": 64})
