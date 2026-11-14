"""Version 2 catalog and research stages over the shared numeric workflow."""
from __future__ import annotations

from uuid import uuid4

import httpx

from p_mc_optimizer.domain.models import CandidatePlan, ExecutionResult, SelectionDecisionV2, parse_catalog_trial
from p_mc_optimizer.llm.research_v2 import AdvisorContextV2, AdvisorContextV21, PlannerContext, validate_output
from p_mc_optimizer.research.contracts import (
    AdvisorPlanV2, ExecutionPolicyV2, ResearchLimits, ResearchQueryPlan, ResearchSnapshot, SearchProblemProfile,
)
from p_mc_optimizer.research.search import SEARCH_ERROR_MESSAGES
from p_mc_optimizer.research.contracts_v21 import AdvisorPlanV21
from p_mc_optimizer.reporting import csv_bytes, json_bytes
from p_mc_optimizer.selection.policies import rank_pilots
from p_mc_optimizer.workflow.graph import Workflow, _code, _error, _model


class CatalogWorkflow(Workflow):
    @property
    def policy(self):
        return ExecutionPolicyV2.model_validate(self.job["policy"])

    @property
    def catalog(self):
        from p_mc_optimizer.catalog import CatalogSnapshot

        return CatalogSnapshot.model_validate_json(json_bytes(self.repo.get_v2_snapshot(self.job_id)["catalog"]))

    @property
    def research(self):
        return ResearchSnapshot.model_validate(self.repo.get_v2_snapshot(self.job_id)["research"])

    @property
    def numeric_execution(self):
        return self.repo.get_v2_snapshot(self.job_id).get("numeric_execution")

    def pipeline(self):
        pipeline = super().pipeline()
        pipeline.insert(pipeline.index("formulate_problem"), "check_settings")
        pipeline.insert(pipeline.index("preflight"), "prepare_candidates")
        pipeline.insert(pipeline.index("filter_solvers"), "run_research")
        return pipeline

    def internal_research(self, operation):
        try:
            url = httpx.URL(self.api_url or "")
            if url.scheme != "http" or url.host != "127.0.0.1":
                raise ValueError("The gateway must use IPv4 loopback")
            research = self.research
            profile = research.search_profile
            seconds = profile.limits.active_timeout_seconds if profile else 180
            if operation == "refresh":
                seconds = research.sync_limits.max_active_seconds + 20
            with httpx.Client(timeout=seconds + 20, trust_env=False, follow_redirects=False) as client:
                response = client.post(
                    str(url).rstrip("/") + "/internal/research/invoke",
                    json={"job_id": self.job_id, "worker_id": self.worker_id, "operation": operation},
                    headers={"X-PMC-Internal-Token": self.token},
                )
            body = response.json()
            return (body, None) if response.is_success else (None, body)
        except (httpx.HTTPError, ValueError):
            return None, _error("SEARCH_OUTCOME_UNKNOWN", "نتیجهٔ درخواست درگاه پژوهش مشخص نیست.")

    def check_settings(self):
        if self.selection_policy == "catalog_fixed":
            return
        _, error = self.internal_research("check")
        if error:
            return self.needs_configuration(error)

    def _remember_catalog_selection(self, selection):
        frozen = self.repo.get_v2_snapshot(self.job_id).get("catalog_readiness") or {}
        eligible = {card.implementation_id for card in selection.cards}
        eligible.update(row["implementation_id"] for row in selection.excluded if row["reason"] == "SHORTLIST_LIMIT")
        self.keep("catalog_selection_summary", {
            "catalog_hash": self.catalog.catalog_hash, "discovered_count": frozen.get("discovered_count"),
            "qualified_count": len(self.catalog.cards), "eligible_count": len(eligible),
            "shortlist_count": len(selection.cards), "source": "frozen_job_snapshot",
        })

    def prepare_candidates(self):
        from p_mc_optimizer.catalog import shortlist

        numeric = self.numeric_execution
        if numeric is not None:
            from p_mc_optimizer.catalog.budget_policy import POLICY_HASH, numeric_runtime_fingerprint_hash
            from p_mc_optimizer.catalog.models import digest

            if numeric["numeric_runtime_fingerprint_sha256"] != numeric_runtime_fingerprint_hash():
                return self.stop("NUMERIC_POLICY_ENVIRONMENT_MISMATCH",
                                 "محیط عددی این فرایند با قرارداد ثبت‌شدهٔ اجرا سازگار نیست.")
            if numeric["policy_hash"] != POLICY_HASH or digest(numeric["policy_definition"]) != POLICY_HASH:
                return self.stop("NUMERIC_POLICY_INVALID", "تعریف سیاست عددی ثبت‌شده با این اجرا سازگار نیست.")

        if self.selection_policy == "catalog_fixed":
            policy = self.policy
            try:
                card = self.catalog.get(policy.fixed_implementation_id, policy.fixed_config_id)
            except (KeyError, ValueError):
                return self.stop("CATALOG_QUALIFICATION_REQUIRED", "پیکربندی ثابت در کاتالوگ تأیید نشده است.")
            if not card:
                return self.stop("CATALOG_QUALIFICATION_REQUIRED", "پیکربندی ثابت در کاتالوگ تأیید نشده است.")
            self.keep("solver_cards", [_model(card)])
            return
        dimension = len(self.revision.spec.variables)
        search = self.research.search_profile
        numeric = self.numeric_execution
        exclusions = {row["base_config_id"]: row["error_code"] for row in numeric["exclusions"]} if numeric else None
        selection = shortlist(self.catalog, dimension=dimension,
                              limit=search.limits.max_solver_cards if search else 20, numeric_exclusions=exclusions)
        self.keep("solver_cards", [_model(card) for card in selection.cards])
        self.keep("catalog_exclusions", [_model(reason) for reason in selection.excluded])
        self._remember_catalog_selection(selection)
        if len({card.implementation_id for card in selection.cards}) < 3:
            return self.stop("CATALOG_CANDIDATES_INSUFFICIENT", "دست‌کم سه پیاده‌سازی سازگار را تأیید کنید.")

    def _role(self, role, schema, context, validate):
        value = context.model_dump(mode="json", exclude={"repair"})
        self.keep(f"role_context:{role}", value)
        return self._invoke(role, value, validate=validate, schema_id=schema)

    def _research_failure(self, error):
        if self.cancelled(immediate=True):
            return self.route_cancel()
        if self.policy.on_research_failure == "continue_with_catalog":
            self.keep("research_error", error)
            self.keep("effective_selection_policy", "catalog_llm_then_pilot")
            self.keep("proposal_source", "catalog_after_research_failure")
            return None
        code = _code(error)
        self.keep("research_error", error)
        self.keep("research_partial", True)
        if code in {"SEARCH_AUTH_FAILED", "SEARCH_RATE_LIMITED"} and error.get("retryable"):
            self.keep("research_outcome", None)
            return self.needs_configuration(error)
        message = SEARCH_ERROR_MESSAGES.get(code) or error.get("message_fa") or (
            "پژوهش برای انتخاب روش به شواهد کافی نرسید."
        )
        self.stop(code or "RESEARCH_EVIDENCE_INSUFFICIENT", message,
                  reason="insufficient_evidence")
        return {"route": "verify"}

    def run_research(self):
        if self.selection_policy != "web_research_then_pilot":
            for stage in ("research_planning", "searching", "fetching_evidence"):
                self.event("skipped_by_policy", stage=stage)
            return
        if self.workflow.get("effective_selection_policy"):
            return
        snapshot = self.research
        source_snapshot = snapshot.source_snapshot
        if not self.workflow.get("query_plan"):
            self.phase("research_planning")
            profile = SearchProblemProfile(
                n_variables=len(self.revision.spec.variables), objective_sense=self.revision.spec.objective.sense,
            )
            limits = snapshot.search_profile.limits if snapshot.search_profile else ResearchLimits()
            context = PlannerContext(
                profile=profile, sources=list(source_snapshot.active_sources), max_queries=limits.max_queries,
            )
            def validate(output):
                result = ResearchQueryPlan.model_validate(output).normalized(source_snapshot)
                if len(result.queries) > context.max_queries:
                    raise ValueError("Query count exceeds policy")
                return result

            plan, error = self._role("research_planner", "research_planner.v2", context, validate)
            if error:
                return self._research_failure(error)
            self.keep("query_plan", plan)
        if not self.workflow.get("research_outcome"):
            self.phase("searching")
            outcome, error = self.internal_research("run")
            if error:
                return self._research_failure(error)
            self.keep("research_outcome", outcome)
            if outcome.get("evidence"):
                self.keep("research_error", None)
                self.keep("research_partial", False)
        outcome = self.workflow["research_outcome"]
        if outcome.get("status") in {"failed", "insufficient_evidence", "cancelled"} or not outcome.get("evidence"):
            reasons = outcome.get("reason_codes") or ["RESEARCH_EVIDENCE_INSUFFICIENT"]
            reason = reasons[0]
            return self._research_failure({
                **_error(reason, SEARCH_ERROR_MESSAGES.get(reason, "شاهد قابل استناد بازیابی نشد.")),
                "retryable": outcome.get("search_attempts", 3) < (
                    snapshot.search_profile.limits.max_search_attempts if snapshot.search_profile else 3
                ),
                "retry_after": outcome.get("retry_after"),
            })

    def filter_solvers(self):
        self.phase("planning")
        plan = self.workflow.get("query_plan")
        if not plan or not self.workflow.get("research_outcome"):
            return
        from p_mc_optimizer.catalog import shortlist

        settings = self.research.search_profile
        numeric = self.numeric_execution
        selection = shortlist(
            self.catalog, dimension=len(self.revision.spec.variables),
            query=" ".join(query["text"] for query in plan["queries"]),
            limit=settings.limits.max_solver_cards if settings else 20,
            numeric_exclusions={row["base_config_id"]: row["error_code"] for row in numeric["exclusions"]} if numeric else None,
        )
        self.keep("solver_cards", [_model(card) for card in selection.cards])
        self.keep("catalog_exclusions", [_model(reason) for reason in selection.excluded])
        self._remember_catalog_selection(selection)
        if len({card.implementation_id for card in selection.cards}) < 3:
            return self.stop("CATALOG_CANDIDATES_INSUFFICIENT", "دست‌کم سه پیاده‌سازی سازگار را تأیید کنید.")

    def propose_candidates(self):
        if self.workflow.get("candidate_plan"):
            return
        cards = self.workflow["solver_cards"]
        if self.selection_policy == "catalog_fixed":
            card = cards[0]
            self.keep("candidate_plan", CandidatePlan.model_validate({
                "candidates": [{
                    "algorithm_id": card["implementation_id"], "config_id": card["config_id"],
                    "rationale_fa": "پیکربندی انتخاب‌شده توسط کاربر", "evidence_refs": [],
                }],
                "uncertainty_notes": None,
            }))
            self.keep("proposal_source", "catalog_fixed")
            return
        wf = self.workflow
        library = self.policy.policy_version == "2.1"
        require_web = (library or self.selection_policy == "web_research_then_pilot") and not wf.get(
            "effective_selection_policy"
        )
        outcome = wf.get("research_outcome") or {}
        evidence = outcome.get("evidence", []) if require_web else []
        spec = _model(self.revision.spec)
        spec.pop("source_map", None)
        profile = dict(wf["profile"])
        profile.pop("sample_artifact_id", None)
        context_values = {
            "problem": spec, "profile": profile, "solver_cards": cards,
            "evidence": evidence, "candidate_count": 3,
        }
        if library:
            schema = "advisor.v2.1"
            plan_type = AdvisorPlanV21
            context = AdvisorContextV21.model_validate({
                **context_values, "require_evidence": require_web,
                "library_snapshot_id": outcome.get("snapshot_id") if require_web else None,
            })
        else:
            schema = "advisor.v2"
            plan_type = AdvisorPlanV2
            context = AdvisorContextV2.model_validate({**context_values, "require_web": require_web})

        def validate(output):
            plan = plan_type.model_validate(output)
            validate_output(schema, plan, context)
            return plan

        plan, error = self._role("advisor", schema, context, validate)
        if plan:
            self.keep("advisor_plan_v2", plan)
            self.repo.record_research_findings(self.job_id, plan.research_findings, worker_id=self.worker_id)
        if error or plan.status != "ready":
            failure = error or _error("RESEARCH_EVIDENCE_INSUFFICIENT", "مشاور سه گزینهٔ دارای شواهد کافی پیدا نکرد.")
            if require_web:
                fallback = self._research_failure(failure)
                if fallback is not None:
                    return fallback
                # One advisor role budget is shared across the original and fallback plans.
                return self.propose_candidates()
            return self.stop(_code(failure), failure["message_fa"], reason="insufficient_evidence")
        self.keep("candidate_plan", CandidatePlan.model_validate({
            "candidates": [
                {"algorithm_id": item.implementation_id, "config_id": item.config_id,
                 "rationale_fa": item.rationale_fa, "evidence_refs": list(item.evidence_refs)}
                for item in plan.candidates
            ],
            "uncertainty_notes": None,
        }))
        if not wf.get("proposal_source"):
            source = "library_research" if library else "web_research"
            self.keep("proposal_source", source if require_web else "catalog_advisor")

    def validate_plan(self):
        plan = self.workflow["candidate_plan"]
        expected = self.policy.pilot_candidate_count or 1
        rows = plan["candidates"]
        if len(rows) != expected or len({r["algorithm_id"] for r in rows}) != expected:
            raise ValueError("Candidate count differs from frozen policy")
        for row in rows:
            self.catalog.get(row["algorithm_id"], row["config_id"])
            if row["config_id"] not in {card["config_id"] for card in self.workflow["solver_cards"]}:
                raise ValueError("Candidate is outside the frozen numeric shortlist")

    def _config(self, implementation_id):
        return next(row["config_id"] for row in self.workflow["candidate_plan"]["candidates"]
                    if row["algorithm_id"] == implementation_id)

    def seed_identity(self, algorithm_id):
        if algorithm_id is None:
            return "none"
        return self.catalog.get(algorithm_id, self._config(algorithm_id)).seed_identity

    def build_trial(self, revision, **fields):
        from p_mc_optimizer.catalog import catalog_trial_request

        implementation_id = fields.pop("algorithm_id")
        trial = catalog_trial_request(
            revision, snapshot=self.catalog, implementation_id=implementation_id,
            config_id=self._config(implementation_id), **fields,
        )
        snapshot = self.numeric_execution
        if snapshot is None:
            return trial
        from p_mc_optimizer.catalog.budget_models import BaseCatalogAuthorization, JobCertificateAuthorization
        from p_mc_optimizer.catalog.trials import apply_budget_configuration

        base_id = trial.config_id
        certified = snapshot["certificates"].get(base_id)
        if certified:
            authorization = JobCertificateAuthorization(kind="job_certificate",
                qualification_id=certified["certificate"]["qualification_id"], **certified)
        else:
            base_report = snapshot["base_qualification_reports"][base_id]
            authorization = BaseCatalogAuthorization(kind="base_catalog", base_config_id=base_id,
                catalog_hash=self.catalog.catalog_hash, qualified_report=base_report["report"],
                qualified_report_hash=base_report["report_hash"])
        return apply_budget_configuration(trial, snapshot, authorization)

    def select_solver(self):
        self.phase("selecting")
        if self.repo.get_decision(self.job_id):
            return
        wf = self.workflow
        plan = CandidatePlan.model_validate(wf["candidate_plan"])
        if self.selection_policy == "catalog_fixed":
            card = plan.candidates[0]
            decision = SelectionDecisionV2(
                decision_id=str(uuid4()), selection_status="fixed_by_policy",
                decision_source="catalog_fixed", selected_algorithm_id=card.algorithm_id,
                selected_config_id=card.config_id, metrics=[], scale=None, tie_break_order=[], uncertainty_notes=None,
            )
        else:
            results = [
                (parse_catalog_trial(row["request"]), ExecutionResult.model_validate(row["outcome"]))
                for row in self.repo.list_executions(self.job_id)
                if row["phase"] == "pilot" and row["outcome"]
            ]
            decision = rank_pilots(
                results, plan, allowed_configs={r["implementation_id"]: r["config_id"] for r in wf["solver_cards"]}
            )
        self.repo.put_decision(self.job_id, decision, worker_id=self.worker_id)

    def can_verify(self, stop):
        return super().can_verify(stop) or (stop.get("status") == "failed" and self.workflow.get("research_partial"))

    def build_report_data(self):
        super().build_report_data()
        wf = self.workflow
        snapshot = self.research.model_dump(mode="json")
        if snapshot.get("search_profile"):
            snapshot["search_profile"].pop("credential_ref", None)
        research = wf.get("research_outcome") or {
            "status": "cancelled" if self.cancelled() else (
                "failed" if wf.get("research_error") else "not_requested"
            ),
        }
        self.report.update({
            "schema_version": self.policy.policy_version,
            "research_snapshot": {**snapshot, "source_policy_hash": self.research.source_snapshot.hash},
            "research": research,
            "research_error": wf.get("research_error"),
            "query_plan": wf.get("query_plan"),
            "advisor_plan_v2": wf.get("advisor_plan_v2"),
            "catalog_exclusions": wf.get("catalog_exclusions"),
            "requested_selection_policy": self.selection_policy,
            "effective_selection_policy": wf.get("effective_selection_policy", self.selection_policy),
            "numeric_execution": self.numeric_execution,
        })
        summary = dict(wf.get("catalog_selection_summary") or {})
        if summary:
            context = wf.get("role_context:advisor") or {}
            sent_ids = [row["implementation_id"] for row in context.get("solver_cards", [])]
            advisor_calls = [call for call in self.report.get("llm_calls", []) if call.get("role") == "advisor"]
            received = any(call.get("status") == "succeeded" for call in advisor_calls)
            nominees = [row["algorithm_id"] for row in (wf.get("candidate_plan") or {}).get("candidates", [])]
            summary.update({
                "sent_count": len(sent_ids) if received else None,
                "sent_implementation_ids": sent_ids if received else [],
                "sent_status": "received_response" if received else "not_confirmed",
                "nominated_count": len(nominees), "nominated_implementation_ids": nominees,
                "not_nominated_implementation_ids": [name for name in sent_ids if name not in nominees] if received else [],
                "pilot_winner_implementation_id": self.report.get("selected_algorithm_id"),
            })
            self.report["catalog_selection_summary"] = summary
        if wf.get("research_partial"):
            self.report["selection_status"] = "insufficient_evidence"
        self.report["timing"]["elapsed_research_seconds"] = research.get("active_seconds")
        self.report["environment"]["catalog_hash"] = self.catalog.catalog_hash

    def additional_artifacts(self, report):
        research = report["research"]
        columns = (
            "source_id", "requested_url", "final_url", "fetch_status", "content_level", "fetched_at",
            "validated_at", "text_sha256",
        )
        if report["schema_version"] == "2.1":
            columns += ("version_id", "observation_id", "stale", "parser_version", "truncated")
        return [
            self.repo.save_artifact(
                self.job_id, "research_json", json_bytes(research), suffix=".json", worker_id=self.worker_id
            ),
            self.repo.save_artifact(
                self.job_id, "sources_csv", csv_bytes(research.get("documents", []), columns),
                suffix=".csv", worker_id=self.worker_id,
            ),
        ]
