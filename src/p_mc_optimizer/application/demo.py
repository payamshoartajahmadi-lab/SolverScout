"""Repeatable university preparation using the existing catalog and source services."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

from filelock import FileLock, Timeout
import psutil

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.catalog.qualification import QUALIFICATION_FE_PER_CONFIG, NUMERIC_POLICY_FE_PER_CONFIG
from p_mc_optimizer.catalog.budget_policy import ALLOWLIST
from p_mc_optimizer.catalog.adapter import environment_id
from p_mc_optimizer.catalog.budget_policy import numeric_runtime_fingerprint, numeric_runtime_fingerprint_hash
from p_mc_optimizer.research.contracts_v21 import LibrarySourceUpdate, SourceSyncRequest
from p_mc_optimizer.research.settings import SourcesUpdate


TARGETS = {
    "mealpy:swarm_based.ABC:OriginalABC": "ABC",
    "mealpy:swarm_based.PSO:OriginalPSO": "PSO",
    "mealpy:evolutionary_based.ES:CMA_ES": "CMA-ES",
    "mealpy:evolutionary_based.DE:OriginalDE": "DE",
    "mealpy:swarm_based.GWO:OriginalGWO": "GWO",
    "mealpy:swarm_based.WOA:OriginalWOA": "WOA",
}
OFFICIAL_SEEDS = (
    "https://mealpy.readthedocs.io/en/latest/pages/general/introduction.html",
    "https://mealpy.readthedocs.io/en/latest/pages/models/mealpy.bio_based.html",
    "https://mealpy.readthedocs.io/en/latest/pages/models/mealpy.evolutionary_based.html",
    "https://mealpy.readthedocs.io/en/latest/pages/models/mealpy.swarm_based.html",
)
MAX_BUDGET = 100_000


def now():
    return datetime.now(UTC).isoformat()


class DemoPreparation:
    def __init__(self, repo, catalog, library, settings, synchronizer):
        self.repo, self.catalog, self.library = repo, catalog, library
        self.settings, self.synchronizer = settings, synchronizer
        self.directory = repo.data_dir / "demo-preparation"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        self.path = self.directory / "manifest.json"

    def _load(self):
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"schema_version": "demo-preparation/1.0", "preparation_id": str(uuid4()),
                "created_at": now(), "status": "not_started", "qualification_budget": None,
                "attempts": [], "requests": {}, "errors": [], "source_sync_runs": []}

    def _save(self, state):
        state["updated_at"] = now()
        temporary = self.path.with_suffix(".part")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def _inventory(self):
        result = self.catalog.list_implementations(limit=250)
        return {row["implementation_id"]: row for row in result["items"] if row["implementation_id"] in TARGETS}

    def _usage(self, state):
        charged = reserved = 0
        for attempt in state["attempts"]:
            if not attempt.get("qualification_id"):
                # The planned reservation precedes the catalog transaction. A crash
                # cannot make its allowance available to another new batch.
                reserved += attempt["quota"]
                continue
            batch = self.catalog.batch(attempt["qualification_id"])
            charged += batch["started_evaluations"] + batch["charged_unknown"]
            reserved += (max(0, batch["max_evaluations"] - batch["started_evaluations"] - batch["charged_unknown"])
                         if batch["status"] == "running" else batch["reserved_active"])
        return charged, reserved

    def _documents(self):
        from p_mc_optimizer.research.extraction import extraction_profile
        from p_mc_optimizer.research.library_retrieval import LibraryRetriever, has_target_evidence

        snapshot = self.settings.get_sources()
        rule = next((r for r in snapshot.sources if r.source_id == "mealpy_docs"), None)
        with self.library.connect() as connection:
            config = self.library.source_config(connection, "mealpy_docs")
        max_chars = min(100_000, config.limits.max_text_chars) if config and config.limits else 100_000
        profile = extraction_profile(max_chars)
        documents = LibraryRetriever(self.library).documents(snapshot, source_ids=["mealpy_docs"], limit=100)
        fresh, texts = [], []
        for row in documents:
            if row.get("stale") or row.get("parser_version") != profile:
                continue
            _, version = self.library.previous_document("mealpy_docs", row["canonical_url"])
            text = self.library.verify_artifacts(version) if version else None
            if text is not None:
                fresh.append(row)
                texts.append(text)
        available = {row["canonical_url"] for row in fresh}
        covered = [identity for identity in TARGETS if any(text and has_target_evidence(text, identity) for text in texts)]
        return {"enabled": bool(rule and rule.enabled), "usable_documents": len(fresh),
                "required_documents_ready": set(OFFICIAL_SEEDS) <= available and len(covered) == len(TARGETS),
                "covered_implementation_ids": covered, "parser_version": profile}

    def status(self):
        with FileLock(str(self.path) + ".lock", timeout=5):
            state = self._load()
        rows = self._inventory()
        methods = [{"name": name, "implementation_id": identity,
                    "status": rows.get(identity, {}).get("status", "missing"),
                    "pilot_500_compatible": bool((rows.get(identity, {}).get("qualification") or {}).get("pilot_500_compatible")),
                    "budget_policy_status": rows.get(identity, {}).get("budget_policy_status", "not_applicable")}
                   for identity, name in TARGETS.items()]
        charged, reserved = self._usage(state)
        source = self._documents()
        methods_ready = all(m["status"] == "qualified" and m["pilot_500_compatible"] and
                            (m["implementation_id"] not in ALLOWLIST or m["budget_policy_status"] == "qualified")
                            for m in methods)
        ready = methods_ready and source["enabled"] and source["required_documents_ready"]
        return {**state, "methods": methods, "target_count": len(TARGETS),
                "prepared_count": sum(m["status"] == "qualified" for m in methods),
                "charged_evaluations": charged, "reserved_evaluations": reserved,
                "official_source": source, "ready": ready}

    def begin(self, *, enable_official_source=False, qualification_budget=MAX_BUDGET, idempotency_key=None):
        if type(qualification_budget) is not int or not 1 <= qualification_budget <= MAX_BUDGET:
            raise ApiError(422, "DEMO_BUDGET_INVALID", "سقف آماده‌سازی باید بین ۱ و ۱۰۰٬۰۰۰ ارزیابی باشد.")
        key = idempotency_key or str(uuid4())
        request = {"enable_official_source": enable_official_source, "qualification_budget": qualification_budget}
        with FileLock(str(self.path) + ".lock", timeout=5):
            state = self._load()
            if state["qualification_budget"] not in (None, qualification_budget):
                raise ApiError(409, "DEMO_BUDGET_FROZEN", "سقف ثبت‌شدهٔ این آماده‌سازی قابل تغییر نیست؛ مصرف قبلی حفظ شده است.")
            if key in state["requests"]:
                if state["requests"][key] != request:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "شناسهٔ آماده‌سازی برای درخواست دیگری ثبت شده است.")
            if state["status"] == "running":
                try:
                    alive = psutil.Process(state["owner_pid"]).create_time() == state["owner_started"]
                except (KeyError, psutil.Error):
                    alive = False
                if alive:
                    return False
            state.update(status="running", errors=[], qualification_budget=qualification_budget,
                         owner_pid=os.getpid(), owner_started=psutil.Process().create_time(),
                         enable_official_source=enable_official_source,
                         environment_id=environment_id(), numeric_runtime_fingerprint=numeric_runtime_fingerprint(),
                         numeric_runtime_fingerprint_sha256=numeric_runtime_fingerprint_hash())
            state["requests"][key] = request
            self._save(state)
        return True

    def _batch(self, state, configs, scope):
        quota = len(configs) * (QUALIFICATION_FE_PER_CONFIG if scope == "base" else NUMERIC_POLICY_FE_PER_CONFIG)
        pending = next((a for a in state["attempts"] if not a.get("qualification_id")), None)
        if pending is None:
            charged, reserved = self._usage(state)
            if charged + reserved + quota > state["qualification_budget"]:
                raise ApiError(409, "DEMO_QUALIFICATION_BUDGET_EXHAUSTED", "بودجهٔ باقی‌مانده برای آزمون کامل پیکربندی‌های مفقود کافی نیست.")
            pending = {"idempotency_key": f"demo:{state['preparation_id']}:{len(state['attempts'])}",
                       "scope": scope, "config_ids": sorted(configs), "quota": quota,
                       "environment_id": state["environment_id"],
                       "numeric_runtime_fingerprint_sha256": state["numeric_runtime_fingerprint_sha256"]}
            state["attempts"].append(pending)
            self._save(state)
        if pending["scope"] != scope or pending["config_ids"] != sorted(configs):
            raise ApiError(409, "DEMO_RESERVATION_CONFLICT", "رزرو ناتمام باید با همان پیکربندی‌ها تعیین تکلیف شود.")
        batch_id, created = self.catalog.reserve_batch(
            pending["config_ids"], max_seconds=120 if scope == "base" else 300,
            scope=scope, idempotency_key=pending["idempotency_key"],
        )
        pending["qualification_id"] = batch_id
        self._save(state)
        if created:
            self.catalog.run_batch(batch_id)
        batch = self.catalog.batch(batch_id)
        if not created and batch["status"] in {"interrupted", "failed", "timed_out", "cancelled"}:
            # A catalog reservation may have committed before the manifest saved
            # its ID. Keep that batch and its charges, then qualify only what is
            # still missing in a new, separately reserved batch.
            rows = self._inventory()
            remaining = [r["config_id"] for r in rows.values() if r["config_id"] in configs and
                         (r["status"] != "qualified" if scope == "base" else r["budget_policy_status"] != "qualified")]
            if remaining:
                return self._batch(state, remaining, scope)
            return
        if batch["status"] != "completed" or len(batch["reports"]) != len(configs) or any(
            report["status"] != "qualified" for report in batch["reports"]
        ):
            raise ApiError(409, "DEMO_QUALIFICATION_FAILED", "همهٔ آزمون‌های سازگاری موفق نشده‌اند؛ جزئیات و مصرف ذخیره شده‌اند.")

    def _enable_source(self):
        snapshot = self.settings.get_sources()
        official = next((r for r in snapshot.sources if r.source_id == "mealpy_docs"), None)
        if official is None or official.hostname != "mealpy.readthedocs.io" or official.path_prefixes != ("/en/latest",):
            raise ApiError(409, "DEMO_OFFICIAL_SOURCE_CHANGED", "مجوز منبع رسمی با تعریف شناخته‌شده یکسان نیست؛ منابع سفارشی تغییر نکردند.")
        if not official.enabled:
            snapshot = self.settings.update_sources(SourcesUpdate(
                expected_revision=snapshot.revision,
                sources=tuple(r.model_copy(update={"enabled": True}) if r.source_id == official.source_id else r
                              for r in snapshot.sources),
            ))
        with self.library.connect() as connection:
            current = self.library.source_config(connection, "mealpy_docs")
        seeds = tuple(dict.fromkeys((*current.seed_urls, *OFFICIAL_SEEDS))) if current else OFFICIAL_SEEDS
        if current is None or seeds != current.seed_urls:
            self.library.configure("mealpy_docs", LibrarySourceUpdate(
                expected_revision=current.revision if current else 0, seed_urls=seeds,
                sitemap_urls=current.sitemap_urls if current else (),
                follow_links=current.follow_links if current else False,
                crawl_depth=current.crawl_depth if current else 0, limits=current.limits if current else None,
            ))

    def _refresh_sources(self, state):
        request = SourceSyncRequest(source_ids=("mealpy_docs",))
        self.synchronizer.recover_sync()
        previous = (self.synchronizer.sync_status(state["source_sync_runs"][-1])
                    if state["source_sync_runs"] else None)
        if previous and previous["status"] in {"queued", "running"}:
            sync = previous
        else:
            sync = self.synchronizer.start_sync(
                request, f"demo:{state['preparation_id']}:{len(state['source_sync_runs'])}",
            )
            if sync["run_id"] not in state["source_sync_runs"]:
                state["source_sync_runs"].append(sync["run_id"])
            self._save(state)
        # Allow the existing 90-second lease to expire after a crashed worker.
        # This bounds waiting; the registered acquisition limits remain unchanged.
        deadline = monotonic() + request.limits.max_active_seconds + 100
        while sync["status"] in {"queued", "running"}:
            if monotonic() >= deadline:
                raise ApiError(409, "SOURCE_SYNC_IN_PROGRESS", "دریافت هنوز پایان نیافته است؛ همان آماده‌سازی را دوباره ادامه دهید.")
            self.synchronizer.recover_sync()
            sync = self.synchronizer.sync_status(sync["run_id"])
            if sync["status"] == "queued":
                self.synchronizer.run_next(str(uuid4()), run_id=sync["run_id"],
                                           stopped=lambda: monotonic() >= deadline)
                sync = self.synchronizer.sync_status(sync["run_id"])
                if sync["status"] == "queued":
                    sleep(0.2)
            elif sync["status"] == "running":
                sleep(0.2)
        if previous and sync["run_id"] == previous["run_id"] and sync["status"] == "interrupted":
            # The old source ledger stays terminal and intact. A fresh run can
            # now acquire its released locks instead of silently relabeling it.
            return self._refresh_sources(state)
        return sync

    def execute(self):
        try:
            with FileLock(str(self.path) + ".work.lock", timeout=0):
                state = self._load()
                try:
                    self.catalog.discover()
                    rows = self._inventory()
                    if set(rows) != set(TARGETS) or any(not r.get("config_id") or r["status"] == "disabled" for r in rows.values()):
                        raise ApiError(409, "DEMO_METHOD_UNAVAILABLE", "یکی از شش روش نصب یا پیکربندی معتبر ندارد؛ وضعیت کاتالوگ را ببینید.")
                    missing = [r["config_id"] for r in rows.values() if r["status"] != "qualified"]
                    if missing:
                        self._batch(state, missing, "base")
                    rows = self._inventory()
                    adapted = [r["config_id"] for identity, r in rows.items()
                               if identity in ALLOWLIST and r["budget_policy_status"] != "qualified"]
                    if adapted:
                        self._batch(state, adapted, "budget_horizon.v1")
                    if state["enable_official_source"]:
                        self._enable_source()
                        if not self._documents()["required_documents_ready"]:
                            self._refresh_sources(state)
                    state["status"] = "completed" if self.status()["ready"] else "blocked"
                    if state["status"] == "blocked":
                        state["errors"] = [{"code": "DEMO_SOURCES_NOT_READY", "message_fa": "منبع رسمی یا اسناد تازهٔ لازم آماده نیست؛ فعال‌سازی و دریافت را بررسی کنید."}]
                except ApiError as exc:
                    state.update(status="blocked", errors=[{"code": exc.error_code, "message_fa": exc.message_fa}])
                except Exception as exc:
                    state.update(status="failed", errors=[{"code": "DEMO_PREPARATION_FAILED", "message_fa": "آماده‌سازی کامل نشد؛ تنظیمات و رزروهای قبلی حفظ شده‌اند.", "error_type": type(exc).__name__}])
                self._save(state)
        except Timeout:
            pass
        return self.status()

    def prepare(self, **kwargs):
        return self.execute() if self.begin(**kwargs) else self.status()

    def start(self, **kwargs):
        if self.begin(**kwargs):
            Thread(target=self.execute, daemon=True, name="university-preparation").start()
        return self.status()


def preparation_service(repo, *, catalog=None, library=None, settings=None, synchronizer=None):
    from p_mc_optimizer.catalog import CatalogStore
    from p_mc_optimizer.llm.credentials import CredentialBroker
    from p_mc_optimizer.research.library_store import LibraryStore
    from p_mc_optimizer.research.library_sync import SourceSynchronizer
    from p_mc_optimizer.research.settings import SearchSettings
    from p_mc_optimizer.research.storage import ResearchStore

    research = ResearchStore(repo.path, repo.data_dir / "artifacts" / "research")
    settings = settings or SearchSettings(research, CredentialBroker(), None)
    library = library or LibraryStore(research, sanitize=settings.sanitize)
    return DemoPreparation(repo, catalog or CatalogStore(repo.path), library, settings,
                           synchronizer or SourceSynchronizer(library, sanitize=settings.sanitize))
