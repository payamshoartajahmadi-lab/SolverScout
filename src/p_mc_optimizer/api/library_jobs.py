"""Server-owned research snapshots and source acquisition for jobs in version 2.1."""

from __future__ import annotations

from datetime import UTC, datetime
from time import sleep

from p_mc_optimizer.api.research_v2 import ResearchJobs, SEARCH_FAILURES_REQUIRING_ACTION
from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.llm.research_v2 import planner_cards
from p_mc_optimizer.research.contracts_v21 import (
    LibrarySourceRef, ResearchQueryPlanV21, ResearchSnapshotV21, SourceSyncRequest,
)
from p_mc_optimizer.research.library_store import LibraryEntry
from p_mc_optimizer.research.source_policy import SourcePolicy, SourcePolicyError
from p_mc_optimizer.research.transport import ResearchError

LIBRARY_BLOCKERS = {"LIBRARY_EMPTY", "LIBRARY_EVIDENCE_STALE", "LOCAL_INDEX_UNAVAILABLE",
                    "SOURCE_SYNC_IN_PROGRESS", "SOURCE_CONFIG_CHANGED"}
SYNC_TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}


class CurrentSourcePolicy(SourcePolicy):
    """Enforce both the original and current source rules at each authorization."""

    __slots__ = ("current",)

    def __init__(self, snapshot, current):
        super().__init__(snapshot)
        object.__setattr__(self, "current", current)

    @property
    def rules(self):
        current = {rule.source_id: rule for rule in self.current().active_sources}
        effective = []
        for frozen in self.snapshot.active_sources:
            live = current.get(frozen.source_id)
            if live is None:
                continue
            if live.matches_hostname(frozen.hostname):
                hostname = frozen.hostname
            elif frozen.matches_hostname(live.hostname):
                hostname = live.hostname
            else:
                continue
            prefixes = set()
            for original_path in frozen.path_prefixes:
                for current_path in live.path_prefixes:
                    if original_path == "/" or current_path.startswith(original_path + "/"):
                        prefixes.add(current_path)
                    elif current_path == "/" or original_path.startswith(current_path + "/") or original_path == current_path:
                        prefixes.add(original_path)
            if prefixes:
                effective.append(frozen.model_copy(update={
                    "hostname": hostname,
                    "include_subdomains": frozen.include_subdomains and live.include_subdomains,
                    "path_prefixes": tuple(sorted(prefixes)),
                }))
        return tuple(effective)

    def authorize(self, url, source_ids=None, **options):
        canonical, source_id = super().authorize(url, source_ids, **options)
        SourcePolicy(self.current()).authorize(canonical, (source_id,), **options)
        return canonical, source_id


class LibraryJobs(ResearchJobs):
    def __init__(self, repo, llm, settings, catalog, library, retriever, retrieval, synchronizer):
        super().__init__(repo, llm, settings, catalog)
        self.library = library
        self.retriever = retriever
        self.retrieval = retrieval
        self.synchronizer = synchronizer

    def freeze(self, request):
        if request.policy.policy_version != "2.1":
            return super().freeze(request)
        catalog = self.catalog.snapshot()
        if catalog.catalog_hash != request.catalog_hash:
            raise ApiError(409, "STALE_CATALOG", "کاتالوگ تغییر کرده است. آن را بازخوانی کنید.")
        sources = self.settings.get_sources(request.source_revision)
        if sources.revision != self.settings.get_sources().revision:
            raise ApiError(409, "SOURCE_CONFIG_CHANGED", "فهرست منابع تغییر کرده است.")
        policy = request.policy
        refs = {ref.source_id: ref.revision for ref in request.source_config_refs}
        if refs:
            selected = tuple(rule for rule in sources.active_sources if rule.source_id in refs)
            if len(selected) != len(refs):
                raise ApiError(422, "SOURCE_NOT_ALLOWED", "یکی از منابع انتخاب‌شده فعال نیست.")
            sources = sources.model_copy(update={"sources": selected})
        configs = self.library.freeze_sources(
            sources, tuple(refs), required=policy.retrieval_mode == "direct_refresh",
        ) if refs else ()
        if any(refs.get(config.source_id) != config.revision for config in configs) or len(configs) != len(refs):
            raise ApiError(409, "SOURCE_CONFIG_CHANGED", "تنظیمات دریافت منبع تغییر کرده است.")
        search_profile = None
        if request.search_provider_ref:
            ref = request.search_provider_ref
            search_profile = self.settings.get_profile(ref.revision)
            if not search_profile or search_profile.profile_id != ref.profile_id:
                raise ApiError(409, "STALE_VERSION", "تنظیمات موتور جست‌وجو تغییر کرده‌اند.")
        snapshot = ResearchSnapshotV21(
            source_snapshot=sources, search_profile=search_profile, source_configs=configs,
            retrieval_mode=policy.retrieval_mode, catalog_hash=catalog.catalog_hash,
            max_evidence_age_hours=policy.max_evidence_age_hours, stale_evidence_policy=policy.stale_evidence_policy,
            on_research_failure=policy.on_research_failure, sync_limits=policy.sync_limits,
        )
        profile = self.llm.profile_snapshot(request.llm_profile.profile_id, request.llm_profile.revision,
                                            require_ready=False)
        return {"catalog": catalog.model_dump(mode="json"), "research": snapshot.model_dump(mode="json"),
                "llm_profile": profile, "numeric_execution": self.catalog.numeric_execution_snapshot(catalog)}

    def _ready(self, job, snapshot):
        self.llm.require_ready(job["llm_profile"])
        if job["workflow"].get("effective_selection_policy"):
            return
        if snapshot.retrieval_mode == "local_only":
            self.retriever.readiness(
                snapshot.source_snapshot, max_age_hours=snapshot.max_evidence_age_hours,
                stale_policy=snapshot.stale_evidence_policy,
            )
        elif snapshot.retrieval_mode == "direct_refresh":
            if not job["workflow"].get("source_sync_run_id"):
                refs = tuple(LibrarySourceRef(source_id=config.source_id, revision=config.revision)
                             for config in snapshot.source_configs)
                self.library.freeze_sources(snapshot.source_snapshot, refs, required=True)
        elif snapshot.retrieval_mode == "web_search":
            if not self.retrieval.get()["web_search_enabled"] or not self.settings.is_ready(snapshot.search_profile):
                raise ApiError(422, "SEARCH_CONFIGURATION_REQUIRED", "موتور جست‌وجو را فعال و اتصال را آزمایش کنید.")
            if snapshot.search_profile.limits.max_queries < 2 or snapshot.search_profile.limits.max_search_attempts < 2:
                raise ApiError(422, "SEARCH_CONFIGURATION_REQUIRED", "برای کشف روش و شواهد پیاده‌سازی دو جست‌وجو لازم است.")
            if not CurrentSourcePolicy(snapshot.source_snapshot, self.settings.get_sources).rules:
                raise ApiError(422, "SEARCH_CONFIGURATION_REQUIRED", "دست‌کم یک منبع فعال انتخاب کنید.")

    def resume(self, job):
        if job["policy"]["policy_version"] == "2.1" and job["blocked_reason"] in (
            LIBRARY_BLOCKERS | SEARCH_FAILURES_REQUIRING_ACTION
        ):
            deadline = (job["error"] or {}).get("retry_after")
            if deadline and datetime.fromisoformat(deadline) > datetime.now(UTC):
                raise ApiError(429, "SEARCH_RATE_LIMITED", "مهلت درخواست بعدی جست‌وجو هنوز نرسیده است.")
            self._ready(job, self._snapshot(job["job_id"]))
        else:
            super().resume(job)

    def _snapshot(self, job_id):
        return ResearchSnapshotV21.model_validate(self.repo.get_v2_snapshot(job_id)["research"])

    def _active(self, job_id, worker_id):
        with self.repo.connect() as db:
            row = self.repo.job_row(db, job_id)
            try:
                self.repo.check_worker(row, worker_id)
            except ApiError:
                return False
            return not row["cancel_requested"]

    def _refresh(self, job, worker_id, snapshot):
        job_id = job["job_id"]
        existing = job["workflow"].get("source_sync_run_id")
        if existing:
            run = self.synchronizer.sync_status(existing)
        else:
            request = SourceSyncRequest(
                source_ids=tuple(config.source_id for config in snapshot.source_configs), limits=snapshot.sync_limits,
            )
            run = self.synchronizer.start_sync(
                request, f"job:{job_id}", owner_job_id=job_id, frozen=snapshot.source_configs,
                source_snapshot=snapshot.source_snapshot,
            )
            self.repo.put_workflow(job_id, "source_sync_run_id", run["run_id"], worker_id=worker_id)
        while run["status"] not in SYNC_TERMINAL:
            if not self._active(job_id, worker_id):
                self.synchronizer.cancel_owned_sync(run["run_id"], job_id)
                raise ApiError(409, "RESEARCH_CANCELLED", "به‌روزرسانی اجرای لغوشده متوقف شد.")
            sleep(0.2)
            run = self.synchronizer.sync_status(run["run_id"])
        try:
            self.retriever.readiness(
                snapshot.source_snapshot, max_age_hours=snapshot.max_evidence_age_hours,
                stale_policy=snapshot.stale_evidence_policy,
            )
        except ApiError as exc:
            run["readiness_error"] = {"error_code": exc.error_code, "message_fa": exc.message_fa}
        return run

    def _web(self, job, worker_id, snapshot, plan):
        job_id = job["job_id"]
        entries = []
        engine = self.settings.gateway
        profile = snapshot.search_profile
        if not self.retrieval.get()["web_search_enabled"]:
            raise ApiError(422, "SEARCH_CONFIGURATION_REQUIRED", "موتور جست‌وجو غیرفعال است.")
        policy = CurrentSourcePolicy(snapshot.source_snapshot, self.settings.get_sources)

        def capture(document, text, raw, title):
            entries.append(LibraryEntry(
                document=document, text=text, raw=raw, title=title,
                discovery={"kind": "tavily", "job_id": job_id},
                http_status=304 if document.fetch_status == "revalidated" else 200,
            ))

        outcome = engine.run(
            job_id, plan.for_web(), policy, profile.limits, credential=self.settings.credential(profile),
            cancelled=lambda: not self._active(job_id, worker_id),
            sanitize=self.settings.sanitize, on_document=capture,
            resume=bool(job["workflow"].get("resume_count") and (
                job["workflow"].get("research_error") or {}
            ).get("error_code") in SEARCH_FAILURES_REQUIRING_ACTION),
        )
        outcome_dict = outcome.model_dump(mode="json", exclude={
            "documents": {"__all__": {"etag", "last_modified"}},
        })
        if outcome.status not in {"completed", "partial"} or not outcome.evidence:
            return outcome_dict, set()
        if not entries:
            # The previous web request is journaled. Reuse its verified bytes or text.
            for doc in outcome.documents:
                if doc.fetch_status not in {"read", "revalidated"}:
                    continue
                value = engine.store.load_text(doc.text_sha256)
                if value is not None:
                    capture(doc, value, None, "")
        for entry in entries:
            doc = entry.document
            policy.authorize(doc.requested_url, (doc.source_id,))
            policy.authorize(doc.final_url, (doc.source_id,))
        if not self._active(job_id, worker_id):
            raise ApiError(409, "RESEARCH_CANCELLED", "پژوهش لغو شد.")
        published = self.library.publish_documents(entries, snapshot.source_snapshot, owner_job_id=job_id)
        return outcome_dict, {row["observation_id"] for row in published}

    def invoke(self, payload):
        job = self.repo.get_job(payload.job_id)
        if job["policy"]["policy_version"] != "2.1":
            return super().invoke(payload)
        if not self._active(payload.job_id, payload.worker_id):
            raise ApiError(409, "INVALID_JOB_STATE", "درخواست پژوهش در این مرحله مجاز نیست.")
        snapshot = self._snapshot(payload.job_id)
        if payload.operation == "check":
            self._ready(job, snapshot)
            return {"ready": True}
        if payload.operation == "refresh":
            if snapshot.retrieval_mode != "direct_refresh" or job["phase"] != "refreshing_sources":
                raise ApiError(409, "INVALID_JOB_STATE", "به‌روزرسانی برای این مرحله مجاز نیست.")
            return self._refresh(job, payload.worker_id, snapshot)
        if job["phase"] not in {"searching", "fetching_evidence"} or not job["workflow"].get("query_plan"):
            raise ApiError(409, "INVALID_JOB_STATE", "برنامهٔ پژوهش این اجرا آماده نیست.")
        plan = ResearchQueryPlanV21.model_validate(job["workflow"]["query_plan"])
        aliases = {card.implementation_id: card.aliases for card in planner_cards(job["workflow"]["solver_cards"])}
        try:
            web, eligible = (None, None)
            if snapshot.retrieval_mode == "web_search":
                web, eligible = self._web(job, payload.worker_id, snapshot, plan)
                if not eligible:
                    return {**web, "schema_version": "2.1", "retrieval_mode": snapshot.retrieval_mode,
                            "evidence": [], "status": "insufficient_evidence" if web["status"] in
                            {"completed", "partial"} else web["status"]}
            local = self.retriever.retrieve(
                payload.job_id, plan, snapshot.source_snapshot, max_age_hours=snapshot.max_evidence_age_hours,
                stale_policy=snapshot.stale_evidence_policy, eligible_observation_ids=eligible,
                aliases=aliases, worker_id=payload.worker_id,
            )
        except (ResearchError, SourcePolicyError) as exc:
            raise ApiError(422, exc.code, "درگاه پژوهش نتوانست درخواست را کامل کند.") from None
        result = {
            "http_requests": 0, "search_attempts": 0, "cost": None,
            **(web or {}), **local, "retrieval_mode": snapshot.retrieval_mode,
        }
        if web:
            result["reason_codes"] = list(dict.fromkeys([*web["reason_codes"], *local["reason_codes"]]))
            if web["status"] == "partial" and result["status"] == "completed":
                result["status"] = "partial"
        if sync_id := job["workflow"].get("source_sync_run_id"):
            result["sync_run"] = self.synchronizer.sync_status(sync_id)
        return result
