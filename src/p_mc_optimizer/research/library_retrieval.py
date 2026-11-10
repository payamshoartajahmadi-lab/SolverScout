"""Offline full-text retrieval with versioned, immutable job evidence."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import UTC, datetime
from unicodedata import normalize

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.persistence.repository import Repository, invalid_state
from p_mc_optimizer.research.contracts_v21 import EvidencePassageV21, ResearchQueryPlanV21
from p_mc_optimizer.research.library_store import normalize_index_text
from p_mc_optimizer.research.source_policy import SourcePolicy, SourcePolicyError
from p_mc_optimizer.research.storage import canonical, digest

RETRIEVAL_VERSION = "library_fts5_unicode61_bm25_2_1_v2.1"
PASSAGE_VERSION = "passage_selection_v2.1"
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_PARAGRAPH = re.compile(r"\S(?:.*?\S)?(?=\n[ \t]*\n|\Z)", re.DOTALL)


def _terms(text):
    return tuple(dict.fromkeys(
        word for word in _TOKEN.findall(normalize_index_text(text)) if len(word) > 1
    ))[:32]


def _match_expression(text, aliases=()):
    phrases = filter(None, (" ".join(_terms(name)) for name in aliases))
    terms = tuple(dict.fromkeys([*phrases, *_terms(text)]))[:32]
    return " OR ".join(f'"{term}"' for term in terms)


def alias_present(text, alias):
    """Require token boundaries and preserve case for short Latin abbreviations."""
    words = _TOKEN.findall(normalize("NFKC", alias))
    if not words:
        return False
    short = len(words) == 1 and re.fullmatch(r"[A-Z]{2,3}", words[0])
    if short:
        haystack, parts = normalize("NFKC", text), words
    else:
        haystack = normalize_index_text(text)
        parts = _TOKEN.findall(normalize_index_text(alias))
    pattern = r"(?<!\w)" + r"[^\w]+".join(map(re.escape, parts)) + r"(?!\w)"
    return re.search(pattern, haystack) is not None


def _query_aliases(query, aliases):
    return tuple(dict.fromkeys(
        name for identifier in query.get("implementation_ids", ())
        for name in aliases.get(identifier, ())
    ))


def _interleave(groups):
    queues = deque(deque(group) for group in groups if group)
    while queues:
        group = queues.popleft()
        yield group.popleft()
        if group:
            queues.append(group)


def _by_source(items):
    sources = defaultdict(list)
    for item in items:
        sources[item[0]["source_id"]].append(item)
    return _interleave(sources.values())


def _passages(document, text, query, aliases):
    terms = set(_terms(query["text"]))
    targets = _query_aliases(query, aliases)
    ranges = []
    for paragraph in _PARAGRAPH.finditer(text):
        start, end = paragraph.span()
        for offset in range(start, end, 1500):
            stop = min(end, offset + 1500)
            snippet = text[offset:stop]
            matches = len(terms.intersection(_TOKEN.findall(normalize_index_text(snippet))))
            named = any(alias_present(snippet, name) for name in targets)
            if matches or named:
                ranges.append((not named, -matches, offset, stop))
    return [(document, text, query["purpose"], -matches, start, end)
            for _, matches, start, end in sorted(ranges)[:2]]


def _select_passages(items, aliases):
    rows = []
    remaining = 18_000
    for document, text, method, matches, start, end in _interleave(
        [_passages(doc, content, query, aliases) for doc, content, query in items]
    ):
        if not remaining or len(rows) == 12:
            break
        end = min(end, start + remaining)
        passage = EvidencePassageV21(
            evidence_id=digest([PASSAGE_VERSION, document["observation_id"], start, end])[:32],
            document_id=document["document_id"], version_id=document["version_id"],
            observation_id=document["observation_id"], source_id=document["source_id"],
            url=document["final_url"], selection_method=method, stale=document["stale"],
            text=text[start:end], start=start, end=end, keyword_matches=matches,
        )
        rows.append(passage.model_dump(mode="json"))
        remaining -= end - start
    return rows


class LibraryRetriever:
    """Reads one SQLite generation per lookup and freezes the job's exact excerpts."""

    def __init__(self, store):
        self.store = store

    @contextmanager
    def _read(self):
        with self.store.connect() as connection:
            connection.execute("BEGIN")
            yield connection

    @staticmethod
    def _ensure_index(connection):
        try:
            connection.execute("SELECT rowid FROM library_fts LIMIT 1").fetchone()
        except sqlite3.OperationalError as error:
            if "no such" not in str(error):
                raise
            raise ApiError(503, "LOCAL_INDEX_UNAVAILABLE", "فهرست جست‌وجوی محلی در دسترس نیست.") from None

    def _candidates(self, connection, snapshot, current, *, source_ids=None,
                    max_age_hours=168, stale_policy="reject", eligible_observation_ids=None,
                    eligible_version_ids=None):
        frozen_policy = SourcePolicy(snapshot)
        current_policy = SourcePolicy(current)
        active = {rule.source_id for rule in snapshot.active_sources}
        active.intersection_update(rule.source_id for rule in current.active_sources)
        if source_ids is not None:
            active.intersection_update(source_ids)
        if not active:
            return
        fields = """
            SELECT m.source_id, m.remote_unavailable, m.last_attempt_at, m.last_successful_validation_at,
                   o.payload AS observation, o.observation_id, v.payload AS version,
                   d.document_id, d.canonical_url
            FROM library_memberships m JOIN library_documents d USING(document_id)
            JOIN library_observations o ON m.source_id = o.source_id AND m.document_id = o.document_id
            JOIN library_versions v ON v.version_id = o.version_id
        """
        parameters = sorted(active)
        clauses = ["m.source_id IN (" + ",".join("?" for _ in active) + ")"]
        for column, selected in [("o.observation_id", eligible_observation_ids),
                                 ("v.version_id", eligible_version_ids)]:
            if selected is not None:
                if not selected:
                    return
                clauses.append(f"{column} IN (" + ",".join("?" for _ in selected) + ")")
                parameters.extend(sorted(selected))
        if eligible_observation_ids is None:
            clauses.append("m.observation_id = o.observation_id")
        rows = connection.execute(
            fields + " WHERE " + " AND ".join(clauses) + " ORDER BY d.canonical_url, o.observation_id",
            parameters,
        )
        now = datetime.now(UTC)
        for row in rows:
            observation, version = json.loads(row["observation"]), json.loads(row["version"])
            if observation.get("raw_available"):
                version["raw_available"] = True
            if version.get("content_level") not in {"abstract", "documentation", "full_text"}:
                continue
            document = observation.get("document") or {}
            if not isinstance(document, dict):
                continue
            url = observation.get("final_url") or row["canonical_url"]
            requested = observation.get("requested_url") or url
            source = (row["source_id"],)
            try:
                for target in (requested, *observation.get("redirect_chain", ()), url):
                    frozen_policy.match(target, source)
                    current_policy.match(target, source)
            except SourcePolicyError:
                continue
            validated = observation.get("validated_at") or document.get("validated_at")
            age = None
            if validated:
                try:
                    timestamp = datetime.fromisoformat(validated)
                    if timestamp.tzinfo is not None and timestamp <= now:
                        age = (now - timestamp).total_seconds() / 3600
                except ValueError:
                    pass
            stale = age is None or age > max_age_hours or bool(row["remote_unavailable"])
            if stale and stale_policy == "reject":
                continue
            yield ({
                **{key: version.get(key) for key in (
                    "version_id", "title", "language", "content_type", "content_level", "parser_version",
                    "bytes_sha256", "text_sha256", "truncated", "published_at", "updated_at",
                )},
                "document_id": row["document_id"], "canonical_url": row["canonical_url"],
                "observation_id": row["observation_id"], "source_id": row["source_id"],
                "source_revision": observation.get("source_revision"),
                "requested_url": requested, "final_url": url,
                "redirect_chain": observation.get("redirect_chain", []),
                "observed_at": observation.get("observed_at"), "validated_at": validated,
                "fetched_at": document.get("fetched_at") or observation.get("observed_at"),
                "last_attempt_at": row["last_attempt_at"],
                "last_successful_validation_at": row["last_successful_validation_at"],
                "fetch_status": document.get("fetch_status", "read")
                if eligible_observation_ids is not None else "cached",
                "cache_age_hours": age, "stale": stale, "remote_unavailable": bool(row["remote_unavailable"]),
                "discovery": observation.get("discovery"),
            }, version)

    def readiness(self, source_snapshot, *, max_age_hours=168, stale_policy="reject"):
        with self._read() as connection:
            self._ensure_index(connection)
            current = self.store.source_snapshot(connection)
            stale = False
            for document, version in self._candidates(
                connection, source_snapshot, current, max_age_hours=max_age_hours,
                stale_policy="allow_with_warning",
            ):
                if not self.store.verify_artifacts(version):
                    continue
                if not document["stale"] or stale_policy == "allow_with_warning":
                    return True
                stale = True
        if stale:
            raise ApiError(409, "LIBRARY_EVIDENCE_STALE", "اسناد کتابخانه را به‌روزرسانی کنید یا استفاده از شواهد قدیمی را مجاز کنید.")
        raise ApiError(409, "LIBRARY_EMPTY", "برای این منابع سند قابل استفاده‌ای وجود ندارد. کتابخانه را به‌روزرسانی کنید.")

    def _rank(self, connection, candidates, queries, aliases, *, limit=20):
        if not candidates:
            return [() for _ in queries], False
        connection.execute("""
            CREATE TEMP TABLE retrieval_scope (
                observation_id TEXT PRIMARY KEY, version_id TEXT, source_id TEXT, canonical_url TEXT
            )
        """)
        connection.executemany(
            "INSERT INTO retrieval_scope VALUES (?, ?, ?, ?)",
            ((row["observation_id"], row["version_id"], row["source_id"], row["canonical_url"])
             for row, _ in candidates),
        )
        by_id = {row["observation_id"]: (row, version) for row, version in candidates}
        verified = {}
        ranked, missing = [], False
        for query in queries:
            expression = _match_expression(query["text"], _query_aliases(query, aliases))
            sources = query["source_ids"]
            if not expression or not sources:
                ranked.append(())
                continue
            matches = connection.execute(
                "SELECT s.observation_id FROM library_fts JOIN retrieval_scope s USING(version_id) "
                "WHERE library_fts MATCH ? AND s.source_id IN (" + ",".join("?" for _ in sources) + ") "
                "ORDER BY bm25(library_fts, 0.0, 2.0, 1.0), s.canonical_url, s.version_id, s.observation_id",
                (expression, *sources),
            )
            result, seen = [], set()
            for item in matches:
                document, version = by_id[item["observation_id"]]
                if document["document_id"] in seen:
                    continue
                artifact_key = (document["version_id"], bool(version.get("raw_available")))
                if artifact_key not in verified:
                    verified[artifact_key] = self.store.verify_artifacts(version)
                content = verified[artifact_key]
                if not content:
                    missing = True
                    continue
                seen.add(document["document_id"])
                result.append((document, content, query))
                if len(result) == limit:
                    break
            ranked.append(result)
        return ranked, missing

    def retrieve(self, job_id, plan, source_snapshot, *, max_age_hours=168, stale_policy="reject",
                 eligible_observation_ids=None, eligible_version_ids=None, aliases=None, worker_id=None):
        aliases = aliases or {}
        with self.store.connect(write=True) as connection:
            if worker_id is not None:
                job = Repository.job_row(connection, job_id)
                Repository.check_worker(job, worker_id)
                if job["cancel_requested"]:
                    raise invalid_state()
            existing = connection.execute(
                "SELECT payload FROM job_library_snapshots WHERE job_id = ?", (str(job_id),)
            ).fetchone()
            if existing:
                return json.loads(existing["payload"])
            plan = ResearchQueryPlanV21.model_validate(plan).normalized(source_snapshot)
            self._ensure_index(connection)
            current = self.store.source_snapshot(connection)
            candidates = list(self._candidates(
                connection, source_snapshot, current, max_age_hours=max_age_hours, stale_policy=stale_policy,
                eligible_observation_ids=eligible_observation_ids, eligible_version_ids=eligible_version_ids,
            ))
            queries = sorted(
                (query.model_dump(mode="json") for query in plan.queries),
                key=lambda query: query["purpose"] != "implementation_evidence",
            )
            ranked, missing = self._rank(connection, candidates, queries, aliases)
            selected = {}
            for document, text, query in _interleave([list(_by_source(group)) for group in ranked]):
                selected.setdefault(document["document_id"], (document, text, query))
                if len(selected) == 6:
                    break
            evidence = _select_passages(selected.values(), aliases)
            snapshot = {
                "schema_version": "2.1", "snapshot_id": str(job_id),
                "generation": self.store.generation(connection),
                "source_revision": source_snapshot.revision, "current_source_revision": current.revision,
                "source_snapshot": source_snapshot.model_dump(mode="json"),
                "current_source_snapshot": current.model_dump(mode="json"),
                "retrieval_version": RETRIEVAL_VERSION, "passage_version": PASSAGE_VERSION,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "max_evidence_age_hours": max_age_hours, "stale_evidence_policy": stale_policy,
                "query_plan": plan.model_dump(mode="json"),
                "documents": [document for document, _, _ in selected.values()], "evidence": evidence,
                "status": "completed" if evidence else "insufficient_evidence",
                "reason_codes": (["LIBRARY_ARTIFACT_MISSING"] if missing else [])
                + ([] if evidence else ["RESEARCH_EVIDENCE_INSUFFICIENT"]),
            }
            if worker_id is not None:
                Repository.check_worker(job, worker_id)
            connection.execute(
                "INSERT INTO job_library_snapshots (job_id, payload) VALUES (?, ?)",
                (str(job_id), canonical(snapshot)),
            )
        return snapshot

    def search(self, query, source_snapshot, *, source_ids=None, limit=20):
        with self._read() as connection:
            self._ensure_index(connection)
            current = self.store.source_snapshot(connection)
            sources = source_ids if source_ids is not None else [rule.source_id for rule in source_snapshot.active_sources]
            candidates = list(self._candidates(
                connection, source_snapshot, current, source_ids=sources, stale_policy="allow_with_warning",
            ))
            ranked, _ = self._rank(connection, candidates, [
                {"text": query, "source_ids": sources, "purpose": "local_search"}
            ], {}, limit=limit)
            return [row | {"preview": text[:350]} for row, text, _ in ranked[0]]

    def documents(self, source_snapshot, *, source_ids=None, limit=20, offset=0):
        with self._read() as connection:
            current = self.store.source_snapshot(connection)
            result, seen = [], set()
            for row, _ in self._candidates(
                connection, source_snapshot, current, source_ids=source_ids, stale_policy="allow_with_warning",
            ):
                document_id = row["document_id"]
                if document_id in seen:
                    continue
                seen.add(document_id)
                if len(seen) > offset:
                    result.append(row)
                if len(result) == limit:
                    break
            return result

    def versions(self, document_id, source_snapshot):
        with self._read() as connection:
            current = self.store.source_snapshot(connection)
            eligible = {
                row["source_id"] for row, _ in self._candidates(
                    connection, source_snapshot, current, stale_policy="allow_with_warning",
                ) if row["document_id"] == document_id
            }
            if not eligible:
                raise ApiError(404, "LIBRARY_DOCUMENT_NOT_FOUND", "سند در منابع مجاز موجود نیست.")
            versions = {}
            for row in connection.execute(
                "SELECT o.payload AS observation, o.source_id, v.payload AS version "
                "FROM library_observations o LEFT JOIN library_versions v USING(version_id) "
                "WHERE o.document_id = ? ORDER BY o.rowid DESC", (document_id,),
            ):
                if row["source_id"] not in eligible or not row["version"]:
                    continue
                observation, version = json.loads(row["observation"]), json.loads(row["version"])
                if observation.get("raw_available"):
                    version["raw_available"] = True
                try:
                    for target in (observation["requested_url"], *observation.get("redirect_chain", ()),
                                   observation["final_url"]):
                        SourcePolicy(source_snapshot).match(target, (row["source_id"],))
                        SourcePolicy(current).match(target, (row["source_id"],))
                except SourcePolicyError:
                    continue
                version_id = version["version_id"]
                if version_id not in versions:
                    if len(versions) == 20:
                        continue
                    versions[version_id] = {
                        **{key: version.get(key) for key in (
                            "version_id", "title", "canonical_url", "content_level", "truncated",
                            "parser_version", "bytes_sha256", "text_sha256",
                        )},
                        "text": self.store.verify_artifacts(version), "observations": [],
                    }
                observations = versions[version_id]["observations"]
                if len(observations) < 50:
                    observations.append({
                        key: observation.get(key) for key in (
                            "observation_id", "source_id", "requested_url", "final_url", "source_revision",
                            "observed_at", "validated_at", "http_status", "status", "discovery",
                        )
                    })
            return list(versions.values())
