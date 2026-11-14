"""Build server-owned snapshots and run research in the credential-owning API process."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.domain.models import ContractModel, Uuid4String
from p_mc_optimizer.research.contracts import (
    ResearchLimits, ResearchOutcome, ResearchQueryPlan, ResearchSnapshot, SourceSnapshot,
)
from p_mc_optimizer.research.source_policy import SourcePolicy
from p_mc_optimizer.research.transport import ResearchError


SEARCH_FAILURES_REQUIRING_ACTION = {"SEARCH_CONFIGURATION_REQUIRED", "SEARCH_AUTH_FAILED", "SEARCH_RATE_LIMITED"}


def public_research(outcome: ResearchOutcome):
    return outcome.model_dump(mode="json", exclude={"documents": {"__all__": {"etag", "last_modified"}}})


class ResearchInvocation(ContractModel):
    job_id: Uuid4String
    worker_id: Uuid4String
    operation: Literal["check", "run", "refresh"]


class ResearchJobs:
    def __init__(self, repo, llm, settings, catalog):
        self.repo = repo
        self.llm = llm
        self.settings = settings
        self.catalog = catalog

    def catalog_readiness(self, snapshot):
        metadata = self.catalog.list_implementations(limit=1)
        if metadata["catalog_hash"] != snapshot.catalog_hash:
            raise ApiError(409, "STALE_CATALOG", "کاتالوگ تغییر کرده است. آن را بازخوانی کنید.")
        return {"catalog_hash": snapshot.catalog_hash, "discovered_count": metadata["discovered_count"],
                "qualified_count": len(snapshot.cards), "source": "admission_snapshot"}

    def freeze(self, request):
        snapshot = self.catalog.snapshot()
        if snapshot.catalog_hash != request.catalog_hash:
            raise ApiError(409, "STALE_CATALOG", "کاتالوگ تغییر کرده است. آن را بازخوانی کنید.")
        policy = request.policy
        numeric = self.catalog.numeric_execution_snapshot(snapshot)
        if policy.selection_policy == "catalog_fixed":
            try:
                card = snapshot.get(policy.fixed_implementation_id, policy.fixed_config_id)
            except (KeyError, ValueError):
                card = None
            if card is None:
                raise ApiError(422, "CATALOG_QUALIFICATION_REQUIRED", "پیکربندی انتخابی تأیید نشده است.")
            excluded = next((row for row in numeric["exclusions"] if row["base_config_id"] == card.config_id), None)
            if excluded:
                raise ApiError(409 if excluded["error_code"].endswith("ENVIRONMENT_MISMATCH") else 422,
                               excluded["error_code"], "پیکربندی انتخابی مجوز تنظیم اجرا با بودجه را ندارد.")
        source_snapshot = SourceSnapshot(revision=1, sources=())
        search_profile = None
        if policy.selection_policy == "web_research_then_pilot":
            if request.source_revision is not None:
                source_snapshot = self.settings.get_sources(request.source_revision)
            if request.search_profile is not None:
                ref = request.search_profile
                search_profile = self.settings.get_profile(ref.revision)
                if search_profile is None or search_profile.profile_id != ref.profile_id:
                    raise ApiError(409, "STALE_VERSION", "نسخهٔ تنظیمات جست‌وجو تغییر کرده است.")
        frozen = ResearchSnapshot(
            source_snapshot=source_snapshot, search_profile=search_profile,
            research_mode=policy.research_mode, catalog_hash=snapshot.catalog_hash,
            max_cache_age_hours=policy.max_cache_age_hours, on_research_failure=policy.on_research_failure,
        )
        ref = request.llm_profile
        profile = self.llm.profile_snapshot(ref.profile_id, ref.revision, require_ready=False) if ref else None
        return {
            "catalog": snapshot.model_dump(mode="json"),
            "research": frozen.model_dump(mode="json"),
            "llm_profile": profile,
            "numeric_execution": numeric,
            "catalog_readiness": self.catalog_readiness(snapshot),
        }

    def _search_ready(self, snapshot):
        if not snapshot.source_snapshot.active_sources:
            raise ApiError(422, "SEARCH_CONFIGURATION_REQUIRED", "دست‌کم یک منبع فعال انتخاب کنید.")
        if snapshot.research_mode == "cache_only":
            if not self.settings.gateway.cache_available(SourcePolicy(snapshot.source_snapshot),
                                                         snapshot.max_cache_age_hours):
                raise ApiError(422, "RESEARCH_EVIDENCE_INSUFFICIENT", "شاهد محلی با عمر مجاز در دسترس نیست.")
        elif snapshot.search_profile is None or not self.settings.is_ready(snapshot.search_profile):
            raise ApiError(422, "SEARCH_CONFIGURATION_REQUIRED", "کلید جست‌وجو را ثبت و اتصال را آزمایش کنید.")

    def resume(self, job):
        if job["policy"]["policy_version"] == "2.0" and job["blocked_reason"] in (
            SEARCH_FAILURES_REQUIRING_ACTION | {"RESEARCH_EVIDENCE_INSUFFICIENT"}
        ):
            snapshot = self.snapshot(job["job_id"])
            error = job["error"] or {}
            deadline = error.get("retry_after")
            if deadline and datetime.fromisoformat(deadline) > datetime.now(UTC):
                raise ApiError(429, "SEARCH_RATE_LIMITED", "مهلت درخواست بعدی جست‌وجو هنوز نرسیده است.")
            self._search_ready(snapshot)
            return
        self.llm.resume_check(job)

    def snapshot(self, job_id):
        return ResearchSnapshot.model_validate(self.repo.get_v2_snapshot(job_id)["research"])

    def invoke(self, payload):
        job_id, worker_id = payload.job_id, payload.worker_id
        with self.repo.connect() as db:
            self.repo.check_worker(self.repo.job_row(db, job_id), worker_id)
        job = self.repo.get_job(job_id)
        if job["cancel_requested"] or job["policy"]["policy_version"] != "2.0":
            raise ApiError(409, "INVALID_JOB_STATE", "درخواست پژوهش در این مرحله مجاز نیست.")
        selection = job["policy"]["selection_policy"]
        if selection == "catalog_fixed":
            raise ApiError(409, "INVALID_JOB_STATE", "سیاست ثابت به پژوهش نیاز ندارد.")
        snapshot = self.snapshot(job_id)
        if payload.operation == "check":
            self.llm.require_ready(job["llm_profile"])
            if selection == "web_research_then_pilot" and not job["workflow"].get("effective_selection_policy"):
                self._search_ready(snapshot)
            return {"ready": True}
        if payload.operation != "run" or selection != "web_research_then_pilot" or job["phase"] not in {
            "searching", "fetching_evidence",
        }:
            raise ApiError(409, "INVALID_JOB_STATE", "درخواست پژوهش در این مرحله مجاز نیست.")

        profile = snapshot.search_profile
        key = self.settings.credential(profile) if snapshot.research_mode == "live" and profile else None
        plan = job["workflow"].get("query_plan")
        if snapshot.research_mode == "live" and not plan:
            raise ApiError(409, "INVALID_JOB_STATE", "برنامهٔ جست‌وجوی همین اجرا در دسترس نیست.")

        def cancelled():
            latest = self.repo.get_job(job_id)
            return latest["cancel_requested"] or latest["owner_id"] != worker_id or (
                latest["lease_expires_at"] or ""
            ) <= datetime.now(UTC).isoformat()

        def phase_changed(phase):
            if not cancelled():
                self.repo.update_job(job_id, worker_id=worker_id, phase=phase,
                                     event_type="phase_changed", event_payload={"phase": phase})

        try:
            outcome = self.settings.gateway.run(
                job_id, ResearchQueryPlan.model_validate(plan) if plan else None,
                SourcePolicy(snapshot.source_snapshot), profile.limits if profile else ResearchLimits(),
                mode=snapshot.research_mode, max_cache_age_hours=snapshot.max_cache_age_hours, credential=key,
                cancelled=cancelled, sanitize=self.settings.sanitize, on_phase=phase_changed,
                resume=bool(job["workflow"].get("resume_count") and (
                    job["workflow"].get("research_error") or {}
                ).get("error_code") in SEARCH_FAILURES_REQUIRING_ACTION),
            )
        except ResearchError as exc:
            status = 409 if exc.code in {"IDEMPOTENCY_CONFLICT", "RESEARCH_ALREADY_RUNNING"} else 422
            raise ApiError(status, exc.code, "درگاه پژوهش نتوانست درخواست را کامل کند.") from None
        return public_research(outcome)

    def router(self):
        router = APIRouter()
        router.add_api_route("/internal/research/invoke", self.invoke_typed, methods=["POST"])
        return router

    def invoke_typed(self, payload: ResearchInvocation):
        return self.invoke(payload)
