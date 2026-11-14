"""Durable, bounded refreshes of user-approved research sources."""

from __future__ import annotations

import heapq
import json
import re
import xml.etree.ElementTree as ET
import zlib
from datetime import timedelta
from time import monotonic
from urllib.parse import urljoin
from uuid import uuid4

from p_mc_optimizer.application.errors import ApiError

from .contracts import SearchHit, SourceSnapshot
from .contracts_v21 import LibrarySourceConfig, SyncLimits
from .fetch import DocumentFetcher
from .library_store import LibraryEntry
from .source_policy import SourcePolicy, SourcePolicyError, canonical_url
from .storage import canonical, digest, now
from .transport import PinnedTransport, ResearchError, Response

_TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}
_LEASE = timedelta(seconds=90)
_BUDGET = "RESEARCH_BUDGET_EXHAUSTED"
_CANCELLED = "RESEARCH_CANCELLED"
_WORKER_STOPPED = "SYNC_WORKER_STOPPED"


def _usage(conn, run_id, source_id=None):
    return dict(conn.execute(
        """SELECT COUNT(*) AS http_requests,
                  COALESCE(SUM(expanded_bytes),0) AS expanded_bytes,
                  COALESCE(SUM(unknown_bytes),0) AS unknown_bytes,
                  COALESCE(SUM(CASE WHEN state='reserved' THEN reserved_bytes ELSE 0 END),0) AS reserved_bytes,
                  COALESCE(SUM(seconds),0) AS network_seconds
           FROM source_sync_invocations WHERE run_id=? AND (? IS NULL OR source_id=?)""",
        (run_id, source_id, source_id),
    ).fetchone())


def _source_limits(limits, config):
    if config.limits is None:
        return limits
    return SyncLimits.model_validate({
        name: min(value, getattr(config.limits, name)) for name, value in limits.model_dump().items()
    })


def _gzip_sitemap(body, limit):
    if not body.startswith(b"\x1f\x8b"):
        return body
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        expanded = decoder.decompress(body, limit + 1)
    except zlib.error:
        raise ResearchError("SOURCE_SITEMAP_INVALID") from None
    if len(expanded) > limit or decoder.unconsumed_tail:
        raise ResearchError("RESEARCH_DATA_LIMIT")
    if not decoder.eof or decoder.unused_data:
        raise ResearchError("SOURCE_SITEMAP_INVALID")
    return expanded


def _sitemap_links(body, maximum):
    try:
        xml = body.decode("utf-8-sig")
        if "\x00" in xml or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.IGNORECASE):
            raise ValueError("External entities are unsupported.")
        root = ET.fromstring(xml)
    except (UnicodeError, ET.ParseError, ValueError):
        raise ResearchError("SOURCE_SITEMAP_INVALID") from None
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"urlset", "sitemapindex"}:
        raise ResearchError("SOURCE_SITEMAP_INVALID")
    wanted = "url" if kind == "urlset" else "sitemap"
    urls = []
    for child in root:
        if child.tag.rsplit("}", 1)[-1] != wanted:
            continue
        for item in child:
            if item.tag.rsplit("}", 1)[-1] == "loc" and item.text:
                urls.append(item.text.strip())
                break
        if len(urls) > maximum:
            break
    return kind, urls


class _Scope:
    """Intersect the frozen rule and the current rule on every redirect."""

    def __init__(self, session):
        self.session = session
        self.sitemap = False

    def authorize(self, url, sources, *, robots=False, robots_host=None):
        session = self.session
        with session.store.connect() as conn:
            current = session.synchronizer.check_scope(conn, session.config)
        if self.sitemap and not robots:
            target, _, _ = canonical_url(url)
            if target not in session.config.sitemap_urls:
                raise SourcePolicyError("The sitemap endpoint was not registered.")
            return target, session.config.source_id
        args = {"robots": robots, "robots_host": robots_host}
        SourcePolicy(current).authorize(url, sources, **args)
        return SourcePolicy(session.frozen).authorize(url, sources, **args)


class _Session:
    def __init__(self, synchronizer, run, worker_id, config, frozen, limits, started, stopped, responses):
        self.synchronizer = synchronizer
        self.store = synchronizer.library
        self.run = run
        self.job_id = run["run_id"]
        self.worker_id = worker_id
        self.config = config
        self.frozen = frozen
        self.batch_limits = limits
        self.limits = _source_limits(limits, config)
        self.started = started
        self.source_started = monotonic()
        self.stopped = stopped
        self.responses = responses
        self.policy = _Scope(self)
        self.sanitize = synchronizer.sanitize
        self.last_fetch_status = None

    def timeout(self):
        if self.stopped():
            raise ResearchError(_WORKER_STOPPED)
        with self.store.connect() as conn:
            self.synchronizer.check_running(conn, self.run, self.worker_id)
        remaining = min(
            self.limits.request_timeout_seconds,
            self.limits.max_source_seconds - (monotonic() - self.source_started),
            self.batch_limits.max_active_seconds - (monotonic() - self.started),
        )
        if remaining <= 0:
            raise ResearchError(_BUDGET)
        return remaining

    def add_active(self, _elapsed):
        # The synchronizer uses one wall clock across network and parsing.
        return None

    def get(self, transport, kind, _key, url, max_bytes, conditional):
        timeout = self.timeout()
        source_id = self.config.source_id
        if url in self.responses:
            previous_kind, previous_conditional, response = self.responses[url]
            if response is None or (kind, conditional) != (previous_kind, previous_conditional):
                raise ResearchError("SOURCE_DUPLICATE_URL")
            if len(response.body) > max_bytes:
                raise ResearchError("RESEARCH_DATA_LIMIT")
            if kind == "fetch":
                self.last_fetch_status = response.status
            return response
        with self.store.connect(write=True) as conn:
            self.synchronizer.check_running(conn, self.run, self.worker_id)
            self.synchronizer.check_scope(conn, self.config)
            source = _usage(conn, self.job_id, source_id)
            batch = _usage(conn, self.job_id)
            limit = min(
                max_bytes,
                self.batch_limits.max_total_bytes - sum(batch[k] for k in ("expanded_bytes", "unknown_bytes", "reserved_bytes")),
                self.limits.max_total_bytes - sum(source[k] for k in ("expanded_bytes", "unknown_bytes", "reserved_bytes")),
            )
            if limit <= 0 or source["http_requests"] >= self.limits.max_http_requests_per_source:
                raise ResearchError(_BUDGET)
            conn.execute(
                """INSERT INTO source_sync_invocations
                   (run_id,source_id,url,kind,state,started_at,reserved_bytes,reserved_seconds)
                   VALUES(?,?,?,?,'reserved',?,?,?)""",
                (self.job_id, source_id, url, kind, now().isoformat(), limit, timeout),
            )
            self.synchronizer.heartbeat(conn, self.job_id, self.started)
        self.responses[url] = (kind, conditional, None)
        started = monotonic()
        try:
            options = {"accept": "application/xml, text/xml, application/gzip"} if (
                kind == "sitemap" and isinstance(transport, PinnedTransport)
            ) else {}
            response = transport.get(url, timeout, limit, conditional, **options)
            if kind == "sitemap" and response.status == 200:
                response = Response(response.status, response.headers, _gzip_sitemap(response.body, limit))
            if len(response.body) > limit:
                raise ResearchError("RESEARCH_DATA_LIMIT")
        except ResearchError as exc:
            self._settle(url, started, "failed", reason=exc.code, unknown=limit)
            raise
        self._settle(url, started, "completed", response=response)
        self.responses[url] = (kind, conditional, response)
        if kind == "fetch":
            self.last_fetch_status = response.status
        return response

    def _settle(self, url, started, state, *, response=None, reason=None, unknown=0):
        with self.store.connect(write=True) as conn:
            row = conn.execute("SELECT worker_id,status FROM source_sync_runs WHERE run_id=?", (self.job_id,)).fetchone()
            if row["worker_id"] != self.worker_id or row["status"] != "running":
                raise ResearchError(_WORKER_STOPPED)
            conn.execute(
                """UPDATE source_sync_invocations
                   SET state=?,seconds=?,expanded_bytes=?,unknown_bytes=?,http_status=?,reason_code=?
                   WHERE run_id=? AND url=? AND state='reserved'""",
                (state, monotonic() - started, len(response.body) if response else 0, unknown,
                 response.status if response else None, reason, self.job_id, url),
            )
            self.synchronizer.heartbeat(conn, self.job_id, self.started)


class SourceSynchronizer:
    def __init__(self, library, transport=None, *, sanitize=None):
        self.library = library
        self.fetcher = DocumentFetcher(transport)
        self.sanitize = sanitize or library.sanitize

    def start_sync(self, request, idempotency_key, *, owner_job_id=None, frozen=None, source_snapshot=None):
        if not 1 <= len(idempotency_key) <= 256 or not idempotency_key.isascii():
            raise ApiError(422, "IDEMPOTENCY_KEY_INVALID", "شناسهٔ به‌روزرسانی معتبر نیست.")
        key = digest([owner_job_id, idempotency_key])
        fingerprint = digest([
            request.model_dump(mode="json"),
            [config.model_dump(mode="json") for config in frozen] if frozen is not None else None,
            source_snapshot.hash if source_snapshot else None,
        ])
        with self.library.connect(write=True) as conn:
            existing = conn.execute("SELECT * FROM source_sync_runs WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "شناسه برای درخواست دیگری ثبت شده است.")
                return self._public(conn, existing)
            current = self.library.source_snapshot(conn)
            snapshot = source_snapshot or current
            configs = frozen if frozen is not None else self.library.freeze_sources(
                snapshot, request.source_ids, required=True, conn=conn,
            )
            if tuple(config.source_id for config in configs) != tuple(request.source_ids):
                raise ApiError(409, "SOURCE_CONFIG_CHANGED", "فهرست منابع با تنظیمات اجرا سازگار نیست.")
            for config in configs:
                if not self.library.configuration_is_current(conn, config, current):
                    raise ApiError(409, "SOURCE_CONFIG_CHANGED", "تنظیمات منبع تغییر کرده است.")
                locked = conn.execute("SELECT run_id FROM source_sync_locks WHERE source_id=?", (config.source_id,)).fetchone()
                if locked:
                    raise ApiError(
                        409, "SOURCE_SYNC_IN_PROGRESS", "این منبع در حال به‌روزرسانی است.",
                        field_errors=[{"source_id": config.source_id, "run_id": locked["run_id"]}],
                    )
            run_id = str(uuid4())
            payload = {
                **request.model_dump(mode="json"), "source_snapshot": snapshot.model_dump(mode="json"),
                "configs": [config.model_dump(mode="json") for config in configs],
            }
            conn.execute(
                """INSERT INTO source_sync_runs
                   (run_id,idempotency_key,request_hash,owner_job_id,status,created_at,payload)
                   VALUES(?,?,?,?,'queued',?,?)""",
                (run_id, key, fingerprint, owner_job_id, now().isoformat(), canonical(payload)),
            )
            conn.executemany(
                "INSERT INTO source_sync_locks VALUES(?,?)",
                [(source_id, run_id) for source_id in request.source_ids],
            )
            return self._public(conn, self._row(conn, run_id))

    @staticmethod
    def _row(conn, run_id):
        row = conn.execute("SELECT * FROM source_sync_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ApiError(404, "SOURCE_SYNC_NOT_FOUND", "اجرای به‌روزرسانی پیدا نشد.")
        return row

    @staticmethod
    def _public(conn, row):
        payload = json.loads(row["payload"])
        summary = json.loads(row["summary"]) if row["summary"] else {}
        sources = []
        for source_id in payload["source_ids"]:
            staged = conn.execute(
                "SELECT COUNT(*) FROM library_staging WHERE run_id=? AND source_id=?", (row["run_id"], source_id),
            ).fetchone()[0]
            sources.append({
                "source_id": source_id, "documents_checked": staged,
                **summary.get("sources", {}).get(source_id, {}),
                "usage": _usage(conn, row["run_id"], source_id),
            })
        return {
            **{name: row[name] for name in (
                "run_id", "version", "status", "created_at", "started_at", "finished_at", "owner_job_id", "generation",
            )},
            "cancel_requested": bool(row["cancel_requested"]),
            "source_ids": payload["source_ids"], "limits": payload["limits"], "sources": sources,
            "usage": {**_usage(conn, row["run_id"]), "active_seconds": row["active_seconds"]},
            "reason_codes": summary.get("reason_codes", []),
        }

    def sync_status(self, run_id):
        with self.library.connect() as conn:
            return self._public(conn, self._row(conn, run_id))

    def list_runs(self, limit=20):
        with self.library.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM source_sync_runs ORDER BY created_at DESC,run_id DESC LIMIT ?", (limit,),
            ).fetchall()
            return [self._public(conn, row) for row in rows]

    def cancel_sync(self, run_id, expected_version):
        with self.library.connect(write=True) as conn:
            row = self._row(conn, run_id)
            if row["version"] != expected_version:
                raise ApiError(409, "STALE_VERSION", "نسخهٔ اجرای به‌روزرسانی تغییر کرده است.")
            self._cancel(conn, row)
            return self._public(conn, self._row(conn, run_id))

    def cancel_owned_sync(self, run_id, job_id):
        with self.library.connect(write=True) as conn:
            row = self._row(conn, run_id)
            if row["owner_job_id"] == job_id:
                self._cancel(conn, row)

    def _cancel(self, conn, row):
        if row["status"] == "queued":
            self._terminal(conn, row, "cancelled", {"reason_codes": ["SYNC_CANCELLED"]})
        elif row["status"] == "running" and not row["cancel_requested"]:
            conn.execute(
                "UPDATE source_sync_runs SET cancel_requested=1,version=version+1 WHERE run_id=?",
                (row["run_id"],),
            )

    def _terminal(self, conn, row, status, summary, *, generation=None, elapsed=None):
        run_id = row["run_id"]
        conn.execute(
            """UPDATE source_sync_invocations SET state='unknown_outcome',
                      unknown_bytes=reserved_bytes,seconds=reserved_seconds
               WHERE run_id=? AND state='reserved'""", (run_id,),
        )
        elapsed = max(
            elapsed if elapsed is not None else row["active_seconds"],
            _usage(conn, run_id)["network_seconds"],
        )
        conn.execute(
            """UPDATE source_sync_runs SET status=?,version=version+1,finished_at=?,worker_id=NULL,
                      lease_until=NULL,generation=?,active_seconds=?,summary=? WHERE run_id=?""",
            (status, now().isoformat(), generation, elapsed, canonical(summary), run_id),
        )
        conn.execute("DELETE FROM library_staging WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM source_sync_locks WHERE run_id=?", (run_id,))

    def recover_sync(self, *, force=False, conn=None):
        if conn is None:
            with self.library.connect(write=True) as own:
                return self.recover_sync(force=force, conn=own)
        rows = conn.execute(
            "SELECT * FROM source_sync_runs WHERE status='running' AND (? OR lease_until <= ?)",
            (force, now().isoformat()),
        ).fetchall()
        for row in rows:
            self._terminal(conn, row, "interrupted", {"reason_codes": ["SYNC_INTERRUPTED"]})
        return len(rows)

    def claim_sync(self, worker_id, run_id=None):
        with self.library.connect(write=True) as conn:
            self.recover_sync(conn=conn)
            if conn.execute("SELECT 1 FROM source_sync_runs WHERE status='running' LIMIT 1").fetchone():
                return None
            row = conn.execute(
                """SELECT * FROM source_sync_runs WHERE status='queued' AND (? IS NULL OR run_id=?)
                   ORDER BY created_at,run_id LIMIT 1""", (run_id, run_id),
            ).fetchone()
            if row is None:
                return None
            timestamp = now()
            conn.execute(
                """UPDATE source_sync_runs SET status='running',version=version+1,worker_id=?,
                          started_at=?,lease_until=? WHERE run_id=?""",
                (worker_id, timestamp.isoformat(), (timestamp + _LEASE).isoformat(), row["run_id"]),
            )
            return dict(self._row(conn, row["run_id"]))

    @staticmethod
    def heartbeat(conn, run_id, started):
        conn.execute(
            "UPDATE source_sync_runs SET lease_until=?,active_seconds=? WHERE run_id=?",
            ((now() + _LEASE).isoformat(), monotonic() - started, run_id),
        )

    def check_scope(self, conn, config):
        current = self.library.source_snapshot(conn)
        if not self.library.configuration_is_current(conn, config, current):
            raise ResearchError("SOURCE_CONFIG_CHANGED")
        return current

    def check_running(self, conn, run, worker_id):
        row = self._row(conn, run["run_id"])
        if row["status"] != "running" or row["worker_id"] != worker_id or row["lease_until"] <= now().isoformat():
            raise ResearchError(_WORKER_STOPPED)
        if row["cancel_requested"]:
            raise ResearchError(_CANCELLED)
        if row["owner_job_id"]:
            job = conn.execute(
                "SELECT cancel_requested,job_status FROM jobs WHERE job_id=?", (row["owner_job_id"],),
            ).fetchone()
            if job is None or job["cancel_requested"] or job["job_status"] in {"cancelled", "failed", "completed"}:
                raise ResearchError(_CANCELLED)

    def _fetch(self, session, url, discovery):
        source_id = session.config.source_id
        cached, version = self.library.previous_document(source_id, url)
        if version and not version["raw_available"]:
            # Imported text is valid evidence, but a direct refresh acquires raw bytes.
            cached = None
        capture = {}

        def collect(response, parsed):
            capture.update(raw=response.body, parsed=parsed)

        session.last_fetch_status = None
        hit = SearchHit(query_id="direct", url=url, canonical_url=url, provider_rank=1, source_id=source_id)
        document, text = self.fetcher.fetch(
            session, hit, (source_id,), cached, capture=collect,
            link_limit=session.limits.max_urls_per_source if session.config.follow_links else 0,
        )
        parsed = capture.get("parsed") or {}
        raw = capture.get("raw")
        if document.fetch_status == "revalidated":
            raw = self.library.load_raw(document.bytes_sha256)
            parsed = version
        if (session.last_fetch_status == 304 and cached and (raw is None or text is None)
                and document.reason_code != "SOURCE_PARSER_REEXTRACTION_REQUIRED"):
            document = document.model_copy(update={
                "fetch_status": "failed", "reason_code": "LIBRARY_ARTIFACT_MISSING", "validated_at": None,
            })
            text = None
        links = tuple(link for link in parsed.get("links", ()) if self.sanitize(link) == link)
        entry = LibraryEntry(
            document, text, raw, title=parsed.get("title", ""), language=parsed.get("language"),
            links=links, discovery=discovery, http_status=session.last_fetch_status, observed_at=now().isoformat(),
        )
        with self.library.connect(write=True) as conn:
            self.check_running(conn, session.run, session.worker_id)
            current = self.check_scope(conn, session.config)
            try:
                self.library.stage(conn, session.job_id, entry, session.frozen, current)
            except ResearchError as exc:
                document = document.model_copy(update={
                    "fetch_status": "failed", "reason_code": exc.code, "validated_at": None,
                })
                self.library.stage(
                    conn, session.job_id, LibraryEntry(
                        document, None, discovery=discovery, http_status=session.last_fetch_status,
                    ), session.frozen, current,
                )
            self.heartbeat(conn, session.job_id, session.started)
        category = "failed"
        if document.fetch_status != "failed":
            if cached is None and version is None:
                category = "new"
            elif version and (
                document.bytes_sha256, document.text_sha256, document.parser_version
            ) == (version["bytes_sha256"], version["text_sha256"], version["parser_version"]):
                category = "unchanged"
            else:
                category = "changed"
        return document, links if category != "failed" else (), category

    def _crawl(self, session, result):
        config = session.config
        limits = session.limits
        visited = set()
        discovered = set()
        pending = []
        seed_links = []
        sources = (config.source_id,)

        def discover(url):
            try:
                url, _ = session.policy.authorize(url, sources)
            except SourcePolicyError:
                result["skipped_urls"] += 1
                return None
            if self.sanitize(url) != url:
                result["skipped_urls"] += 1
                return None
            if url in discovered:
                return None
            if len(discovered) >= limits.max_urls_per_source:
                result["budget_limited"] = True
                return None
            discovered.add(url)
            result["discovered_urls"] = len(discovered)
            return url

        def queue_links(links, depth, parent):
            for link in sorted(links):
                try:
                    candidate = discover(urljoin(parent, link))
                except ValueError:
                    result["skipped_urls"] += 1
                    continue
                if candidate is not None:
                    heapq.heappush(pending, (depth + 1, candidate, "internal_link", parent))

        def fetch(url, depth, kind, parent=None):
            if url in visited:
                return
            if len(visited) >= limits.max_documents_per_source:
                raise ResearchError(_BUDGET)
            session.timeout()
            visited.add(url)
            result["documents_checked"] = len(visited)
            document, links, category = self._fetch(session, url, {"kind": kind, "parent_url": parent})
            result[category] += 1
            if document.reason_code:
                result["reason_codes"].append(document.reason_code)
                if document.reason_code in {_BUDGET, _WORKER_STOPPED, "LIBRARY_STORAGE_FULL"}:
                    raise ResearchError(document.reason_code)
            if links and config.follow_links and depth < config.crawl_depth:
                if kind == "direct_seed":
                    seed_links.append((links, document.final_url))
                else:
                    queue_links(links, depth, document.final_url)

        for seed in config.seed_urls:
            if url := discover(seed):
                fetch(url, 0, "direct_seed")
        for sitemap in config.sitemap_urls:
            session.timeout()
            session.policy.sitemap = True
            try:
                response, final_url, _ = self.fetcher._follow(
                    session, sitemap, sources, "sitemap", limits.max_sitemap_bytes, {},
                )
            except ResearchError as exc:
                if exc.code in {_BUDGET, _CANCELLED, _WORKER_STOPPED}:
                    raise
                result["reason_codes"].append(exc.code)
                continue
            finally:
                session.policy.sitemap = False
            if response.status != 200:
                result["reason_codes"].append("SOURCE_SITEMAP_UNREACHABLE")
                continue
            try:
                kind, links = _sitemap_links(response.body, limits.max_urls_per_source)
            except ResearchError as exc:
                result["reason_codes"].append(exc.code)
                continue
            if kind == "sitemapindex":
                # Child endpoints are fetched only if explicitly configured.
                result["skipped_urls"] += sum(link not in config.sitemap_urls for link in links)
                continue
            for link in links:
                if url := discover(link):
                    heapq.heappush(pending, (0, url, "sitemap", final_url))
        for links, parent in seed_links:
            queue_links(links, 0, parent)
        while pending:
            depth, url, kind, parent = heapq.heappop(pending)
            fetch(url, depth, kind, parent)

    def run_next(self, worker_id=None, *, run_id=None, stopped=lambda: False):
        if stopped():
            return None
        worker_id = worker_id or str(uuid4())
        run = self.claim_sync(worker_id, run_id)
        if run is None:
            return None
        started = monotonic()
        payload = json.loads(run["payload"])
        frozen = SourceSnapshot.model_validate(payload["source_snapshot"])
        limits = SyncLimits.model_validate(payload["limits"])
        results = {}
        responses = {}
        reason = None
        try:
            for value in payload["configs"]:
                config = LibrarySourceConfig.model_validate(value)
                result = {
                    "documents_checked": 0, "discovered_urls": 0, "skipped_urls": 0, "new": 0,
                    "changed": 0, "unchanged": 0, "failed": 0, "budget_limited": False, "reason_codes": [],
                }
                results[config.source_id] = result
                session = _Session(self, run, worker_id, config, frozen, limits, started, stopped, responses)
                try:
                    self._crawl(session, result)
                except ResearchError as exc:
                    if exc.code in {_CANCELLED, _WORKER_STOPPED}:
                        raise
                    result["reason_codes"].append(exc.code)
                    result["budget_limited"] |= exc.code == _BUDGET
                with self.library.connect(write=True) as conn:
                    self.check_running(conn, run, worker_id)
                    conn.execute(
                        "UPDATE source_sync_runs SET summary=? WHERE run_id=?",
                        (canonical({"sources": results}), run["run_id"]),
                    )
                if monotonic() - started >= limits.max_active_seconds:
                    reason = _BUDGET
                    break
            if stopped():
                reason = _WORKER_STOPPED
        except ResearchError as exc:
            reason = exc.code
        except Exception:
            # Reserved calls may have reached the network. Finalize their charges
            # before surfacing the worker failure to the supervisor.
            self._finish(run, worker_id, results, _WORKER_STOPPED, started)
            raise
        return self._finish(run, worker_id, results, reason, started)

    def _finish(self, run, worker_id, results, reason, started):
        with self.library.connect(write=True) as conn:
            row = self._row(conn, run["run_id"])
            if row["status"] != "running" or row["worker_id"] != worker_id:
                return self._public(conn, row)
            try:
                self.check_running(conn, run, worker_id)
            except ResearchError as exc:
                reason = exc.code
            generation = None
            reasons = [reason] if reason else []
            if reason in {_CANCELLED, _WORKER_STOPPED}:
                status = "cancelled" if reason == _CANCELLED else "interrupted"
            else:
                valid = []
                current = self.library.source_snapshot(conn)
                for value in json.loads(row["payload"])["configs"]:
                    config = LibrarySourceConfig.model_validate(value)
                    result = results.get(config.source_id)
                    if not self.library.configuration_is_current(conn, config, current):
                        reasons.append("SOURCE_CONFIG_CHANGED")
                        if result:
                            result["new"] = result["changed"] = result["unchanged"] = 0
                            result["reason_codes"].append("SOURCE_CONFIG_CHANGED")
                        continue
                    valid.append(config.source_id)
                    if result:
                        reasons.extend(result["reason_codes"])
                        if result["budget_limited"]:
                            reasons.append(_BUDGET)
                if conn.execute("SELECT 1 FROM library_staging WHERE run_id=? LIMIT 1", (run["run_id"],)).fetchone():
                    conn.execute("SAVEPOINT sync_publication")
                    try:
                        generation, observations = self.library.publish_staged(
                            conn, run["run_id"], owner_job_id=row["owner_job_id"], sources=valid,
                        )
                    except ResearchError as exc:
                        conn.execute("ROLLBACK TO sync_publication")
                        conn.execute("RELEASE sync_publication")
                        self._terminal(
                            conn, row, "failed", {"sources": results, "reason_codes": [exc.code]},
                            elapsed=monotonic() - started,
                        )
                        return self._public(conn, self._row(conn, run["run_id"]))
                    conn.execute("RELEASE sync_publication")
                    if not observations:
                        generation = None
                successful = sum(result["new"] + result["changed"] + result["unchanged"] for result in results.values())
                status = ("partial" if reasons else "completed") if successful else "failed"
                if not successful and not reasons:
                    reasons.append("SOURCE_EMPTY")
            summary = {
                "sources": results,
                "reason_codes": list(dict.fromkeys(
                    "SYNC_BUDGET_EXHAUSTED" if code == _BUDGET else code for code in reasons
                )),
            }
            self._terminal(conn, row, status, summary, generation=generation, elapsed=monotonic() - started)
            return self._public(conn, self._row(conn, run["run_id"]))
