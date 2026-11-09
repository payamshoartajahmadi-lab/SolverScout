"""Immutable research configuration and a write-only provider key."""

from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import uuid4
from time import monotonic

from pydantic import Field, SecretStr

from p_mc_optimizer.application.errors import ApiError

from .contracts import ContractModel, FrozenArray, ResearchLimits, SearchProfile, SourceRule, SourceSnapshot
from .search import TAVILY_ORIGIN
from .source_policy import SourcePolicy, SourcePolicyError
from .transport import ResearchError


class ProfileUpdate(ContractModel):
    expected_revision: Annotated[int, Field(ge=1)]
    provider_id: Literal["tavily"] = "tavily"
    limits: ResearchLimits


class SourcesUpdate(ContractModel):
    expected_revision: Annotated[int, Field(ge=1)]
    sources: Annotated[FrozenArray[SourceRule], Field(max_length=300)]


class CredentialTarget(ContractModel):
    revision: Annotated[int, Field(ge=1)]
    generation: Annotated[int, Field(ge=0)]


class CredentialUpdate(CredentialTarget):
    storage_mode: Literal["session", "os_keyring"] = "session"
    secret: Annotated[SecretStr, Field(min_length=1, max_length=4096)]


def _conflict():
    return ApiError(409, "STALE_RESEARCH_SETTINGS", "تنظیمات پژوهش تغییر کرده‌اند. صفحه را تازه کنید.")


class SearchSettings:
    def __init__(self, store, broker, gateway):
        self.store = store
        self.llm_broker = broker
        self.broker = broker.for_service("search")
        self.gateway = gateway
        with self.store.connect(write=True) as conn:
            if self._latest(conn, "search_profiles") is None:
                ref = str(uuid4())
                profile = SearchProfile(profile_id=str(uuid4()), revision=1, credential_ref=ref)
                conn.execute("INSERT INTO search_profiles VALUES(1,?)", (profile.model_dump_json(),))
                conn.execute("INSERT INTO search_credentials(credential_ref) VALUES(?)", (ref,))
            if self._latest(conn, "research_source_revisions") is None:
                sources = SourceSnapshot(revision=1, sources=(
                    SourceRule(
                        source_id="mealpy_docs", label="مستندات MEALPY", hostname="mealpy.readthedocs.io",
                        path_prefixes=("/en/latest",), source_kind="official_docs", enabled=False,
                    ),
                    SourceRule(
                        source_id="mealpy_repository", label="مخزن MEALPY", hostname="github.com",
                        path_prefixes=("/thieu1995/mealpy",), source_kind="code_repository", enabled=False,
                    ),
                    SourceRule(
                        source_id="arxiv_abstracts", label="چکیده‌های arXiv", hostname="arxiv.org",
                        path_prefixes=("/abs",), source_kind="paper_repository", enabled=False,
                    ),
                ))
                conn.execute(
                    "INSERT INTO research_source_revisions VALUES(1,?)", (sources.model_dump_json(),),
                )

    @staticmethod
    def _latest(conn, table):
        return conn.execute(f"SELECT * FROM {table} ORDER BY revision DESC LIMIT 1").fetchone()

    def sanitize(self, value):
        return self.llm_broker.sanitize(self.broker.sanitize(value))

    def _get(self, table, revision, model, code):
        with self.store.connect() as conn:
            row = self._latest(conn, table) if revision is None else conn.execute(
                f"SELECT payload FROM {table} WHERE revision=?", (revision,),
            ).fetchone()
        if row is None:
            raise ApiError(404, code, "نسخهٔ تنظیمات پژوهش پیدا نشد.")
        return model.model_validate_json(row["payload"])

    def get_profile(self, revision=None):
        return self._get("search_profiles", revision, SearchProfile, "SEARCH_PROFILE_NOT_FOUND")

    def get_sources(self, revision=None):
        return self._get("research_source_revisions", revision, SourceSnapshot, "RESEARCH_SOURCE_REVISION_NOT_FOUND")

    def _safe_data(self, data):
        if self.sanitize(data) != data:
            raise ApiError(422, "SECRET_IN_RESEARCH_SETTINGS", "کلید فقط در بخش ویژهٔ کلید ثبت می‌شود.")

    def update_profile(self, update: ProfileUpdate):
        self._safe_data(update.model_dump(mode="json"))
        with self.store.connect(write=True) as conn:
            row = self._latest(conn, "search_profiles")
            current = SearchProfile.model_validate_json(row["payload"])
            if current.revision != update.expected_revision:
                raise _conflict()
            profile = current.model_copy(update={"revision": current.revision + 1, "limits": update.limits})
            conn.execute("INSERT INTO search_profiles VALUES(?,?)", (profile.revision, profile.model_dump_json()))
        return self.public_profile()

    def update_sources(self, update: SourcesUpdate):
        self._safe_data(update.model_dump(mode="json"))
        with self.store.connect(write=True) as conn:
            current = self._latest(conn, "research_source_revisions")
            if current["revision"] != update.expected_revision:
                raise _conflict()
            snapshot = SourceSnapshot(revision=current["revision"] + 1, sources=update.sources)
            conn.execute(
                "INSERT INTO research_source_revisions VALUES(?,?)",
                (snapshot.revision, snapshot.model_dump_json()),
            )
        return snapshot

    @staticmethod
    def _state(conn, profile):
        return conn.execute(
            "SELECT * FROM search_credentials WHERE credential_ref=?", (profile.credential_ref,),
        ).fetchone()

    def credential(self, profile):
        with self.store.connect() as conn:
            state = self._state(conn, profile)
        if state is None:
            return None
        return self.broker.get(profile.credential_ref, TAVILY_ORIGIN, state["mode"])

    def is_ready(self, profile):
        with self.store.connect() as conn:
            state = self._state(conn, profile)
        return bool(
            state and state["tested_generation"] == state["generation"]
            and self.broker.get(profile.credential_ref, TAVILY_ORIGIN, state["mode"])
        )

    def public_profile(self):
        with self.store.connect() as conn:
            profile = SearchProfile.model_validate_json(self._latest(conn, "search_profiles")["payload"])
            state = self._state(conn, profile)
        available = bool(self.broker.get(profile.credential_ref, TAVILY_ORIGIN, state["mode"]))
        return {
            "profile": profile.model_dump(mode="json", exclude={"credential_ref"}),
            "credential_generation": state["generation"],
            "credential_available": available,
            "storage_mode": state["mode"],
            "ready": available and state["tested_generation"] == state["generation"],
            "os_keyring_available": self.broker.os_keyring_available,
        }

    def _target(self, conn, target):
        profile = SearchProfile.model_validate_json(self._latest(conn, "search_profiles")["payload"])
        state = self._state(conn, profile)
        if profile.revision != target.revision or state["generation"] != target.generation:
            raise _conflict()
        return profile, state

    def mutate_credential(self, target):
        key = target.secret.get_secret_value().strip() if isinstance(target, CredentialUpdate) else None
        mode = target.storage_mode if isinstance(target, CredentialUpdate) else None
        if key is not None and (
            not key or not key.isascii() or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in key)
            or self.llm_broker.sanitize(key) != key
        ):
            raise ApiError(422, "SEARCH_INVALID_CREDENTIAL", "کلید جست‌وجو معتبر نیست.")
        previous = None
        with self.store.connect(write=True) as conn:
            profile, state = self._target(conn, target)
            previous = self.broker.get(profile.credential_ref, TAVILY_ORIGIN, state["mode"])
            self.broker.replace(profile.credential_ref, TAVILY_ORIGIN, state["mode"], mode, key)
            try:
                conn.execute(
                    """UPDATE search_credentials SET mode=?,generation=generation+1,tested_generation=NULL
                       WHERE credential_ref=?""",
                    (mode, profile.credential_ref),
                )
                conn.commit()
            except BaseException:
                self.broker.replace(profile.credential_ref, TAVILY_ORIGIN, mode, state["mode"], previous)
                raise
        return self.public_profile()

    def test(self, target: CredentialTarget, idempotency_key: str):
        self._safe_data(idempotency_key)
        with self.store.connect() as conn:
            profile, state = self._target(conn, target)
            sources = SourceSnapshot.model_validate_json(self._latest(conn, "research_source_revisions")["payload"])
        if not sources.active_sources:
            raise ApiError(422, "SEARCH_CONFIGURATION_REQUIRED", "دست‌کم یک منبع پژوهش را فعال کنید.")
        credential = self.broker.get(profile.credential_ref, TAVILY_ORIGIN, state["mode"])
        if not credential:
            raise ApiError(422, "SEARCH_CONFIGURATION_REQUIRED", "کلید جست‌وجو را ثبت کنید.")
        rule = sources.active_sources[0]
        scope = "test:" + profile.profile_id
        payload = {"profile_revision": target.revision, "generation": target.generation, "sources": sources.hash}
        try:
            existing = self.store.reserve(scope, idempotency_key, "test", payload, None)
        except ResearchError:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "این شناسهٔ درخواست قبلاً استفاده شده است.") from None
        if existing:
            if existing["state"] == "reserved":
                return {"ok": False, "status": "unknown_outcome", "code": "SEARCH_OUTCOME_UNKNOWN"}
            return json.loads(existing["payload"])
        started = monotonic()
        try:
            hits = self.gateway.search.search(
                "bounded continuous optimization methods", (rule.hostname,), 1,
                profile.limits.request_timeout_seconds, credential, self.sanitize,
            )
            policy = SourcePolicy(sources)
            count = 0
            for hit in hits:
                try:
                    policy.authorize(hit["url"], (rule.source_id,))
                    count += 1
                except SourcePolicyError:
                    pass
            result = {"ok": True, "status": "completed", "allowed_results": count, "cost": None}
            with self.store.connect(write=True) as conn:
                conn.execute(
                    """UPDATE search_credentials SET tested_generation=generation
                       WHERE credential_ref=? AND generation=?""",
                    (profile.credential_ref, target.generation),
                )
        except ResearchError as exc:
            result = {
                "ok": False, "status": "failed", "code": exc.code, "retry_after": exc.retry_after, "cost": None,
            }
        status = "completed" if result["ok"] else "failed"
        self.store.settle(scope, idempotency_key, status, result, monotonic() - started)
        return result
