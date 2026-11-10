"""Immutable document artifacts, source observations and atomic index publication."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.domain.models import DomainError

from .contracts import SourceDocument, SourceSnapshot
from .contracts_v21 import LibrarySourceConfig
from .source_policy import SourcePolicy, SourcePolicyError
from .storage import canonical, digest, now
from .transport import ResearchError


DEFAULT_STORAGE_LIMIT = 2 * 1024**3
MAX_STORAGE_LIMIT = 10 * 1024**3


def normalize_index_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).translate(
        str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک"}),
    ).casefold()


def _stamp(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.isoformat(timespec="microseconds")


@dataclass(frozen=True)
class LibraryEntry:
    document: SourceDocument
    text: str | None
    raw: bytes | None = None
    title: str = ""
    language: str | None = None
    links: tuple[str, ...] = ()
    discovery: dict = field(default_factory=dict)
    http_status: int | None = None
    observed_at: str | None = None


def _rule(snapshot, source_id):
    return next((rule for rule in snapshot.active_sources if rule.source_id == source_id), None)


def _scope(rule):
    return (rule.hostname, rule.include_subdomains, rule.path_prefixes) if rule else None


class LibraryStore:
    def __init__(self, research_store, *, sanitize=None):
        self.research_store = research_store
        self.path = research_store.path
        self.artifact_dir = research_store.artifact_dir / "library"
        self.artifact_dir.mkdir(exist_ok=True)
        self.sanitize = sanitize or (lambda value: value)

    def connect(self, *, write=False):
        return self.research_store.connect(write=write)

    @staticmethod
    def source_snapshot(conn, revision=None):
        if revision is None:
            row = conn.execute(
                "SELECT payload FROM research_source_revisions ORDER BY revision DESC LIMIT 1",
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT payload FROM research_source_revisions WHERE revision=?", (revision,),
            ).fetchone()
        if not row:
            raise ApiError(404, "RESEARCH_SOURCE_REVISION_NOT_FOUND", "نسخهٔ منابع پیدا نشد.")
        return SourceSnapshot.model_validate_json(row["payload"])

    @staticmethod
    def source_config(conn, source_id, revision=None):
        if revision is None:
            row = conn.execute(
                "SELECT payload FROM library_source_configs WHERE source_id=? ORDER BY revision DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT payload FROM library_source_configs WHERE source_id=? AND revision=?",
                (source_id, revision),
            ).fetchone()
        return LibrarySourceConfig.model_validate_json(row["payload"]) if row else None

    def configuration_is_current(self, conn, config, current):
        latest = self.source_config(conn, config.source_id)
        if latest != config:
            return False
        original = self.source_snapshot(conn, config.source_revision)
        current_rule = _rule(current, config.source_id)
        return current_rule is not None and _scope(current_rule) == _scope(_rule(original, config.source_id))

    def freeze_sources(self, snapshot, source_ids, *, required=False, conn=None):
        """Freeze exact configuration revisions before admitting a refresh."""
        if conn is None:
            with self.connect() as own:
                return self.freeze_sources(snapshot, source_ids, required=required, conn=own)
        current = self.source_snapshot(conn)
        configs = []
        for ref in source_ids:
            source_id = ref if isinstance(ref, str) else ref.source_id
            revision = None if isinstance(ref, str) else ref.revision
            config = self.source_config(conn, source_id, revision)
            if config is None and not required and revision is None:
                continue
            if config is None or not self.configuration_is_current(conn, config, current):
                raise ApiError(409, "SOURCE_CONFIG_CHANGED", "تنظیمات دریافت منبع را دوباره بررسی کنید.")
            if _scope(_rule(snapshot, source_id)) != _scope(_rule(current, source_id)):
                raise ApiError(409, "SOURCE_CONFIG_CHANGED", "مجوز منبع تغییر کرده است.")
            configs.append(config)
        return tuple(configs)

    def configure(self, source_id, update):
        with self.connect(write=True) as conn:
            snapshot = self.source_snapshot(conn)
            old = self.source_config(conn, source_id)
            if update.expected_revision != (old.revision if old else 0):
                raise ApiError(409, "SOURCE_CONFIG_CHANGED", "تنظیمات منبع تغییر کرده است.")
            try:
                config = update.to_config(source_id, snapshot.revision).check_source(snapshot)
            except DomainError as exc:
                raise ApiError(422, exc.code, "نشانی دریافت باید در محدودهٔ منبع مجاز باشد.") from None
            payload = config.model_dump(mode="json")
            if self.sanitize(payload) != payload:
                raise ApiError(422, "SECRET_IN_RESEARCH_SETTINGS", "نشانی منبع نباید حاوی کلید باشد.")
            conn.execute(
                "INSERT INTO library_source_configs VALUES(?,?,?)",
                (source_id, config.revision, config.model_dump_json()),
            )
        return payload

    def list_sources(self):
        with self.connect() as conn:
            snapshot = self.source_snapshot(conn)
            sources = []
            for rule in snapshot.sources:
                config = self.source_config(conn, rule.source_id)
                summary = conn.execute(
                    """SELECT MAX(last_successful_validation_at) AS last_successful_validation_at,
                              MAX(last_attempt_at) AS last_attempt_at,
                              SUM(observation_id IS NOT NULL AND remote_unavailable=0) AS usable_documents
                       FROM library_memberships WHERE source_id=?""", (rule.source_id,),
                ).fetchone()
                latest = conn.execute(
                    """SELECT o.payload FROM library_memberships m
                       JOIN library_observations o ON o.observation_id=m.last_attempt_observation_id
                       WHERE m.source_id=? ORDER BY m.last_attempt_at DESC, o.observation_id DESC LIMIT 1""",
                    (rule.source_id,),
                ).fetchone()
                sources.append({
                    "rule": rule.model_dump(mode="json"),
                    "config": config.model_dump(mode="json") if config else None,
                    "needs_review": bool(config and not self.configuration_is_current(conn, config, snapshot)),
                    **dict(summary),
                    "usable_documents": (summary["usable_documents"] or 0) if rule.enabled else 0,
                    "last_error": json.loads(latest["payload"]).get("reason_code") if latest else None,
                })
        return {"source_revision": snapshot.revision, "sources": sources}

    @staticmethod
    def generation(conn):
        return conn.execute("SELECT COALESCE(MAX(generation),0) FROM library_generations").fetchone()[0]

    @staticmethod
    def fts_available(conn):
        return conn.execute("SELECT 1 FROM sqlite_master WHERE name='library_fts'").fetchone() is not None

    def _artifact(self, hashed, suffix):
        if not isinstance(hashed, str) or len(hashed) != 64 or any(c not in "0123456789abcdef" for c in hashed):
            return None
        return self.artifact_dir / (hashed + suffix)

    def _load_artifact(self, hashed, suffix):
        path = self._artifact(hashed, suffix)
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == hashed else None

    def load_text(self, hashed):
        data = self._load_artifact(hashed, ".txt")
        if data is not None:
            try:
                return data.decode("utf-8")
            except UnicodeError:
                return None
        return None

    def load_raw(self, hashed):
        return self._load_artifact(hashed, ".bin")

    def verify_artifacts(self, version):
        if version.get("raw_available") and self.load_raw(version["bytes_sha256"]) is None:
            return None
        return self.load_text(version["text_sha256"])

    def _storage_limit(self, conn):
        row = conn.execute("SELECT payload FROM library_settings WHERE key='storage_limit_bytes'").fetchone()
        return json.loads(row["payload"]) if row else DEFAULT_STORAGE_LIMIT

    def storage_usage(self, conn=None):
        if conn is None:
            with self.connect() as own:
                return self.storage_usage(own)
        return {
            "used_bytes": sum(path.stat().st_size for path in self.artifact_dir.iterdir() if path.is_file()),
            "limit_bytes": self._storage_limit(conn),
        }

    def set_storage_limit(self, value):
        if type(value) is not int or not 1 <= value <= MAX_STORAGE_LIMIT:
            raise ApiError(422, "LIBRARY_STORAGE_LIMIT_INVALID", "سقف فضای کتابخانه معتبر نیست.")
        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO library_settings VALUES('storage_limit_bytes',?)", (canonical(value),),
            )
            return self.storage_usage(conn)

    def _write_artifact(self, conn, hashed, suffix, data):
        path = self._artifact(hashed, suffix)
        if path is None or hashlib.sha256(data).hexdigest() != hashed:
            raise ResearchError("LIBRARY_ARTIFACT_MISSING")
        if self._load_artifact(hashed, suffix) is not None:
            return
        usage = self.storage_usage(conn)
        if usage["used_bytes"] + len(data) > usage["limit_bytes"]:
            raise ResearchError("LIBRARY_STORAGE_FULL")
        pending = self.artifact_dir / (str(uuid4()) + ".tmp")
        try:
            with pending.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            pending.replace(path)
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise ResearchError("LIBRARY_STORAGE_FULL") from None
            raise
        finally:
            pending.unlink(missing_ok=True)

    def _prepare(self, conn, entry, frozen, current, *, origin, origin_id):
        doc = entry.document
        ids = (doc.source_id,)
        for policy in (SourcePolicy(frozen), SourcePolicy(current)):
            try:
                requested, _ = policy.authorize(doc.requested_url, ids)
                if doc.fetch_status != "failed":
                    for url in (doc.final_url, *doc.redirect_chain):
                        policy.authorize(url, ids)
            except SourcePolicyError:
                raise ResearchError("SOURCE_NOT_ALLOWED") from None
        document_id = digest(requested)
        observed_at = _stamp(entry.observed_at or doc.validated_at or now())
        safe_doc = doc.model_dump(mode="json")
        if self.sanitize(safe_doc) != safe_doc or self.sanitize(entry.discovery) != entry.discovery:
            raise ResearchError("LIBRARY_SECRET_CONTENT")
        version = None
        if doc.fetch_status != "failed":
            raw_text = entry.raw.decode("latin-1") if entry.raw is not None else None
            if entry.text is None or self.sanitize(entry.text) != entry.text or (
                raw_text is not None and self.sanitize(raw_text) != raw_text
            ):
                raise ResearchError("LIBRARY_SECRET_CONTENT")
            version_id = digest([requested, doc.bytes_sha256, doc.text_sha256, doc.parser_version])
            version = {
                "version_id": version_id, "canonical_url": requested,
                "title": self.sanitize(entry.title)[:500], "language": self.sanitize(entry.language),
                "content_type": doc.content_type, "content_level": doc.content_level,
                "bytes_sha256": doc.bytes_sha256, "text_sha256": doc.text_sha256,
                "parser_version": doc.parser_version, "truncated": doc.truncated,
                "published_at": _stamp(doc.published_at), "updated_at": _stamp(doc.updated_at),
                "raw_available": entry.raw is not None,
                "links": [link for link in entry.links if self.sanitize(link) == link],
            }
            self._write_artifact(conn, doc.text_sha256, ".txt", entry.text.encode("utf-8"))
            if entry.raw is not None:
                self._write_artifact(conn, doc.bytes_sha256, ".bin", entry.raw)
        observation = {
            "observation_id": digest([origin, origin_id, doc.source_id, requested, observed_at, doc.document_id]),
            "document_id": document_id, "version_id": version["version_id"] if version else None,
            "source_id": doc.source_id, "source_revision": frozen.revision, "current_source_revision": current.revision,
            "source_rule": _rule(frozen, doc.source_id).model_dump(mode="json"),
            "requested_url": requested, "final_url": doc.final_url, "redirect_chain": list(doc.redirect_chain),
            "observed_at": observed_at, "validated_at": _stamp(doc.validated_at), "http_status": entry.http_status,
            "raw_available": entry.raw is not None,
            "status": doc.fetch_status, "reason_code": doc.reason_code, "discovery": entry.discovery,
            "document": safe_doc,
        }
        return {"version": version, "observation": observation}

    def stage(self, conn, run_id, entry, frozen, current):
        payload = self._prepare(conn, entry, frozen, current, origin="sync", origin_id=run_id)
        observation = payload["observation"]
        conn.execute(
            "INSERT INTO library_staging VALUES(?,?,?,?)",
            (run_id, observation["source_id"], observation["requested_url"], canonical(payload)),
        )

    def _publish(self, conn, entries, *, origin, origin_id=None, owner_job_id=None):
        new = [item for item in entries if not conn.execute(
            "SELECT 1 FROM library_observations WHERE observation_id=?",
            (item["observation"]["observation_id"],),
        ).fetchone()]
        if not new:
            return self.generation(conn), [item["observation"] for item in entries]
        generation = conn.execute(
            "INSERT INTO library_generations(created_at,origin,origin_id) VALUES(?,?,?)",
            (_stamp(now()), origin, origin_id),
        ).lastrowid
        indexed = self.fts_available(conn)
        for item in new:
            observation = item["observation"]
            version = item["version"]
            document_id = observation["document_id"]
            conn.execute(
                "INSERT OR IGNORE INTO library_documents VALUES(?,?)", (document_id, observation["requested_url"]),
            )
            if version:
                text = self.verify_artifacts(version)
                if text is None:
                    raise ResearchError("LIBRARY_ARTIFACT_MISSING")
                result = conn.execute(
                    "INSERT OR IGNORE INTO library_versions VALUES(?,?,?,?,?)",
                    (version["version_id"], document_id, version["bytes_sha256"], version["text_sha256"], canonical(version)),
                )
                if result.rowcount and indexed:
                    conn.execute(
                        "INSERT INTO library_fts(rowid,version_id,title,body) VALUES(?,?,?,?)",
                        (result.lastrowid, version["version_id"],
                         normalize_index_text(version["title"]), normalize_index_text(text)),
                    )
            conn.execute(
                "INSERT INTO library_observations VALUES(?,?,?,?,?,?,?,?)",
                (observation["observation_id"], observation["source_id"], document_id, observation["version_id"],
                 origin_id if origin == "sync" else None, owner_job_id, generation, canonical(observation)),
            )
            self._update_membership(conn, observation, generation)
        return generation, [item["observation"] for item in entries]

    @staticmethod
    def _update_membership(conn, observation, generation):
        key = (observation["source_id"], observation["document_id"])
        old = conn.execute(
            "SELECT * FROM library_memberships WHERE source_id=? AND document_id=?", key,
        ).fetchone()
        attempt_at = observation["observed_at"]
        successful_at = observation["validated_at"] if observation["version_id"] else None
        last_attempt_id = observation["observation_id"]
        last_success_id = observation["observation_id"] if successful_at else None
        unavailable = observation["http_status"] in {404, 410}
        if old:
            if old["last_attempt_at"] > attempt_at:
                attempt_at = old["last_attempt_at"]
                last_attempt_id = old["last_attempt_observation_id"]
                unavailable = bool(old["remote_unavailable"])
            elif not successful_at and not unavailable:
                unavailable = bool(old["remote_unavailable"])
            if not successful_at or (
                old["last_successful_validation_at"] and old["last_successful_validation_at"] > successful_at
            ):
                successful_at = old["last_successful_validation_at"]
                last_success_id = old["observation_id"]
        conn.execute(
            """INSERT INTO library_memberships VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id,document_id) DO UPDATE SET
                 observation_id=excluded.observation_id,
                 last_attempt_observation_id=excluded.last_attempt_observation_id,
                 last_attempt_at=excluded.last_attempt_at,
                 last_successful_validation_at=excluded.last_successful_validation_at,
                 remote_unavailable=excluded.remote_unavailable,generation=excluded.generation""",
            (*key, last_success_id, last_attempt_id, attempt_at, successful_at, int(unavailable), generation),
        )

    def publish_staged(self, conn, run_id, *, owner_job_id=None, sources=None):
        entries = [
            json.loads(row["payload"]) for row in conn.execute(
                "SELECT source_id,payload FROM library_staging WHERE run_id=? ORDER BY source_id,requested_url", (run_id,),
            ) if sources is None or row["source_id"] in sources
        ]
        return self._publish(conn, entries, origin="sync", origin_id=run_id, owner_job_id=owner_job_id)

    def publish_documents(self, entries, source_snapshot, *, owner_job_id=None, origin="tavily"):
        with self.connect(write=True) as conn:
            current = self.source_snapshot(conn)
            prepared = [
                self._prepare(conn, entry, source_snapshot, current, origin=origin, origin_id=owner_job_id)
                for entry in entries
            ]
            _, observations = self._publish(conn, prepared, origin=origin, origin_id=owner_job_id,
                                            owner_job_id=owner_job_id)
            return observations

    def previous_document(self, source_id, url):
        with self.connect() as conn:
            row = conn.execute(
                """SELECT o.payload AS observation,v.payload AS version FROM library_memberships m
                   JOIN library_documents d USING(document_id)
                   JOIN library_observations o ON o.observation_id=m.observation_id
                   JOIN library_versions v ON v.version_id=o.version_id
                   WHERE m.source_id=? AND d.canonical_url=?""", (source_id, url),
            ).fetchone()
        if row is None:
            return None, None
        observation = json.loads(row["observation"])
        version = json.loads(row["version"])
        version["raw_available"] |= observation.get("raw_available", False)
        return SourceDocument.model_validate(observation["document"]), version

    @staticmethod
    def public_observation(payload):
        document = {k: v for k, v in payload["document"].items() if k not in {"etag", "last_modified"}}
        return {**payload, "document": document}

    def list_documents(self, *, source_id=None, limit=50, offset=0):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT m.*,d.canonical_url,v.payload AS version
                   FROM library_memberships m JOIN library_documents d USING(document_id)
                   LEFT JOIN library_observations o ON o.observation_id=m.observation_id
                   LEFT JOIN library_versions v ON v.version_id=o.version_id
                   WHERE (? IS NULL OR m.source_id=?)
                   ORDER BY m.last_attempt_at DESC,d.canonical_url,m.source_id LIMIT ? OFFSET ?""",
                (source_id, source_id, limit, offset),
            ).fetchall()
        return [{**dict(row), "version": json.loads(row["version"]) if row["version"] else None} for row in rows]

    def document_versions(self, document_id, *, limit=50, offset=0):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT o.payload AS observation,v.payload AS version FROM library_observations o
                   LEFT JOIN library_versions v USING(version_id) WHERE o.document_id=?
                   ORDER BY o.generation DESC,o.observation_id LIMIT ? OFFSET ?""", (document_id, limit, offset),
            ).fetchall()
        return [
            {"observation": self.public_observation(json.loads(row["observation"])),
             "version": json.loads(row["version"]) if row["version"] else None}
            for row in rows
        ]

    def import_legacy(self):
        """Import verified v2 text without making historical fetches look fresh."""
        with self.connect() as conn:
            current = self.source_snapshot(conn)
            rows = conn.execute(
                """SELECT d.payload,s.payload AS frozen FROM source_documents d
                   JOIN job_v2_snapshots s USING(job_id) ORDER BY d.job_id,d.document_id""",
            ).fetchall()
        imported = skipped = 0
        for row in rows:
            doc = SourceDocument.model_validate_json(row["payload"])
            if doc.fetch_status not in {"read", "revalidated"} or doc.validated_at is None:
                skipped += 1
                continue
            text = self.research_store.load_text(doc.text_sha256)
            if text is None:
                skipped += 1
                continue
            snapshot = SourceSnapshot.model_validate(json.loads(row["frozen"])["research"]["source_snapshot"])
            try:
                self.publish_documents(
                    [LibraryEntry(doc, text, discovery={"kind": "legacy_import"}, observed_at=_stamp(doc.validated_at))],
                    snapshot, origin="legacy_import",
                )
                imported += 1
            except ResearchError as exc:
                if exc.code not in {"SOURCE_NOT_ALLOWED", "LIBRARY_SECRET_CONTENT"}:
                    raise
                skipped += 1
        return {"verified": imported, "skipped": skipped, "source_revision": current.revision}

    def purge_unreferenced(self):
        with self.connect(write=True) as conn:
            hashes = set()
            for row in conn.execute("SELECT bytes_sha256,text_sha256 FROM library_versions"):
                hashes.update(row)
            for row in conn.execute("SELECT payload FROM library_staging"):
                version = json.loads(row["payload"])["version"]
                if version:
                    hashes.update((version["bytes_sha256"], version["text_sha256"]))
            removed = 0
            for path in self.artifact_dir.iterdir():
                if path.suffix == ".tmp" or (path.suffix in {".bin", ".txt"} and path.stem not in hashes):
                    path.unlink(missing_ok=True)
                    removed += 1
            return {"removed": removed, **self.storage_usage(conn)}
