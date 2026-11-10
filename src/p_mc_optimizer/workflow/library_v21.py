"""Library-backed research over the version 2 numerical pipeline."""

from p_mc_optimizer.llm.research_v2 import PlannerContextV21, planner_cards, validate_output
from p_mc_optimizer.research.contracts import SearchProblemProfile
from p_mc_optimizer.research.contracts_v21 import ExecutionPolicyV21, ResearchQueryPlanV21, ResearchSnapshotV21
from p_mc_optimizer.research.search import SEARCH_ERROR_MESSAGES
from p_mc_optimizer.workflow.catalog_v2 import CatalogWorkflow
from p_mc_optimizer.workflow.graph import _code, _error


class LibraryWorkflow(CatalogWorkflow):
    @property
    def policy(self):
        return ExecutionPolicyV21.model_validate(self.job["policy"])

    @property
    def research(self):
        return ResearchSnapshotV21.model_validate(self.repo.get_v2_snapshot(self.job_id)["research"])

    def pipeline(self):
        pipeline = super().pipeline()
        if self.policy.retrieval_mode == "direct_refresh":
            pipeline.insert(pipeline.index("preflight"), "refresh_library")
        return pipeline

    def refresh_library(self):
        if self.workflow.get("source_sync"):
            return
        self.phase("refreshing_sources")
        run, error = self.internal_research("refresh")
        if error:
            if _code(error) in {"SOURCE_SYNC_IN_PROGRESS", "SOURCE_CONFIG_CHANGED"}:
                return self.needs_configuration(error)
            return self._research_failure(error)
        self.keep("source_sync", run)
        if error := run.get("readiness_error"):
            return self._research_failure(error)

    def run_research(self):
        if self.workflow.get("effective_selection_policy"):
            return
        snapshot = self.research
        if not self.workflow.get("query_plan"):
            self.phase("research_planning")
            profile = SearchProblemProfile(
                n_variables=len(self.revision.spec.variables), objective_sense=self.revision.spec.objective.sense,
            )
            limits = snapshot.search_profile.limits if snapshot.search_profile else None
            context = PlannerContextV21(
                profile=profile, sources=list(snapshot.source_snapshot.active_sources),
                retrieval_mode=snapshot.retrieval_mode, max_queries=limits.max_queries if limits else 3,
                solver_cards=planner_cards(self.workflow["solver_cards"]),
            )
            schema = "research_planner.v2.1"

            def validate(output):
                plan = ResearchQueryPlanV21.model_validate(output).normalized(snapshot.source_snapshot, limits)
                validate_output(schema, plan, context)
                return plan

            plan, error = self._role("research_planner", schema, context, validate)
            if error:
                return self._research_failure(error)
            self.keep("query_plan", plan)
        outcome = self.workflow.get("research_outcome")
        if outcome is None:
            self.phase("searching" if snapshot.retrieval_mode == "web_search" else "fetching_evidence")
            outcome, error = self.internal_research("run")
            if error:
                return self._research_failure(error)
            self.keep("research_outcome", outcome)
        if outcome.get("status") not in {"completed", "partial"} or not outcome.get("evidence"):
            reasons = outcome.get("reason_codes") or ["RESEARCH_EVIDENCE_INSUFFICIENT"]
            code = reasons[0]
            error = _error(code, SEARCH_ERROR_MESSAGES.get(code, "برای این مسئله شاهد قابل استفاده پیدا نشد."))
            if snapshot.search_profile and code in {"SEARCH_AUTH_FAILED", "SEARCH_RATE_LIMITED"}:
                limits = snapshot.search_profile.limits
                error["retryable"] = (outcome["search_attempts"] < limits.max_search_attempts
                                      and outcome["http_requests"] < limits.max_http_requests)
                error["retry_after"] = outcome.get("retry_after")
            return self._research_failure(error)

    def filter_solvers(self):
        # The same qualified cards constrain both the planner and advisor.
        self.phase("planning")

    def build_report_data(self):
        super().build_report_data()
        research = self.report["research"]
        research.setdefault("retrieval_mode", self.policy.retrieval_mode)
        if sync := self.workflow.get("source_sync"):
            research["sync_run"] = sync
        if sync_id := self.workflow.get("source_sync_run_id"):
            research["sync_run_id"] = sync_id
