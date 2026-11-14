"""FastAPI-owned provider profiles, credentials, and durable call accounting."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

import httpx
from pydantic import ValidationError

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.llm.credentials import CredentialBroker
from p_mc_optimizer.llm.models import (
    ROLE_CONTEXTS,
    SCHEMA_IDS,
    CredentialMutationTarget,
    CredentialStatus,
    ExplainerDecisionSummary,
    Invocation,
    ProfileUpdate,
    ProviderSnapshot,
    SecretSubmission,
    origin_of,
)
from p_mc_optimizer.llm.provider import (
    PROMPT_VERSION, call_provider, error_detail, freeze_prompt_bundle, prepare_provider_request, prompt_bundle_manifest,
)

# Provider calls must never log transport headers, even in debug runs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_ROLE_PHASES = {"formulator": "formulating", "advisor": "planning", "research_planner": "research_planning", "explainer": "reporting", "report_reviewer": "reporting"}
_LLM_POLICIES = {
    "llm_only", "llm_shortlist_then_pilot", "catalog_llm_then_pilot", "web_research_then_pilot",
    "evidence_research_then_pilot",
}
_REPORT_FIELDS = {
    "selected_algorithm_id", "selection_status", "solution_status", "solution_origin",
    "verification", "partial_result", "budget", "optimality_status", "objective_raw", "decision_summary",
}
_PRIVATE_FIELDS = {
    "api_key", "authorization", "credential_ref", "cookie", "internal_token",
    "x_pmc_internal_token", "f_star", "x_star", "reference_optimum", "reference_value",
    "reference_solution", "reference_point", "known_optimum", "benchmark_winner",
    "benchmark_reference", "system_prompt", "prompt_system", "provider_snapshot",
    "llm_profile", "evaluation_history", "population_history",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _from_json(value):
    return None if value is None else json.loads(value)


def _stale() -> ApiError:
    return ApiError(409, "STALE_VERSION", "نسخهٔ تنظیمات یا نسل کلید تغییر کرده است. فرم را دوباره باز کنید.")


def _config_required() -> ApiError:
    detail = error_detail("PROVIDER_CONFIGURATION_REQUIRED")
    return ApiError(422, detail["error_code"], detail["message_fa"])


def _invalid_job() -> ApiError:
    return ApiError(409, "INVALID_JOB_STATE", "این نقش در وضعیت فعلی اجرا مجاز نیست.")


def _idempotency_conflict() -> ApiError:
    return ApiError(409, "IDEMPOTENCY_CONFLICT", "این شناسه پیش‌تر با دادهٔ متفاوت استفاده شده است.")


def _check_retry_after(error) -> None:
    retry_after = (_from_json(error) or {}).get("retry_after")
    if retry_after and retry_after > _now():
        detail = error_detail("PROVIDER_RATE_LIMITED", retryable=True)
        raise ApiError(
            429, detail["error_code"],
            f"{detail['message_fa']} زمان تلاش بعدی به UTC: {retry_after}",
            retryable=True,
        )


def _validate_context_fields(value, *, depth: int = 0) -> None:
    if depth > 32:
        raise ApiError(422, "INVALID_SCHEMA", "ساختار دادهٔ نقش بیش از حد تو‌در‌تو است.")
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold().replace("-", "_") in _PRIVATE_FIELDS:
                raise ApiError(422, "INVALID_SCHEMA", "زمینهٔ نقش دارای دادهٔ غیرمجاز است.")
            _validate_context_fields(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_context_fields(child, depth=depth + 1)


class LLMGateway:
    """Provider access goes through this singleton in the API process."""

    def __init__(self, repo, *, transport: httpx.BaseTransport | None = None, internal_token: str | None = None):
        freeze_prompt_bundle()
        self.repo = repo
        self._transport = transport
        self._broker = CredentialBroker(internal_token=internal_token)
        self._profile_lock = RLock()
        self.reconcile()

    @property
    def broker(self):
        return self._broker

    def payload_contains_secret(self, value) -> bool:
        return self._broker.sanitize(value) != value

    def reconcile(self) -> None:
        # A previous process may have sent the reserved call before stopping.
        with self.repo.transaction() as conn:
            conn.execute(
                "UPDATE llm_calls SET status='outcome_unknown', error=?, finished_at=? WHERE status='reserved'",
                (_json(error_detail("LLM_OUTCOME_UNKNOWN")), _now()),
            )

    @staticmethod
    def _active_row(conn):
        return conn.execute(
            """SELECT p.* FROM provider_profiles p JOIN active_provider_profile a
               USING (profile_id, revision) WHERE a.singleton=1"""
        ).fetchone()

    @staticmethod
    def _profile_row(conn, profile_id: str, revision: int):
        return conn.execute(
            "SELECT * FROM provider_profiles WHERE profile_id=? AND revision=?", (profile_id, revision)
        ).fetchone()

    @staticmethod
    def _credential_row(conn, ref: str):
        return conn.execute(
            "SELECT * FROM credential_refs WHERE credential_ref=?", (ref,)
        ).fetchone()

    @staticmethod
    def _snapshot(row) -> ProviderSnapshot:
        values = _from_json(row["settings"])
        values.update(profile_id=row["profile_id"], revision=row["revision"], credential_ref=row["credential_ref"])
        return ProviderSnapshot.model_validate({**values, "provider_snapshot_hash": _hash(values)})

    def _check_credential(self, conn, snapshot: ProviderSnapshot):
        credential = self._credential_row(conn, snapshot.credential_ref)
        if credential is None or credential["origin"] != origin_of(snapshot.base_url):
            raise _config_required()
        return credential

    def _read_key(self, credential) -> str | None:
        return self._broker.get(credential["credential_ref"], credential["origin"], credential["storage_mode"])

    def _status(self, snapshot: ProviderSnapshot, credential) -> CredentialStatus:
        mode = credential["storage_mode"]
        available = self._read_key(credential) is not None
        return CredentialStatus(
            profile_id=snapshot.profile_id,
            profile_revision=snapshot.revision,
            credential_generation=credential["generation"],
            configured=mode is not None,
            available=available,
            storage_mode=mode,
            error="PROVIDER_CONFIGURATION_REQUIRED" if mode is not None and not available else None,
        )

    @staticmethod
    def _latest_test(conn, snapshot: ProviderSnapshot, generation: int):
        return conn.execute(
            """SELECT * FROM llm_calls WHERE purpose='connection_test' AND profile_id=?
               AND profile_revision=? AND credential_generation=? ORDER BY started_at DESC, rowid DESC LIMIT 1""",
            (snapshot.profile_id, snapshot.revision, generation),
        ).fetchone()

    @staticmethod
    def _test_status(test) -> tuple[str, str | None]:
        if test is None:
            return "not_tested", None
        status = {"succeeded": "passed", "reserved": "pending"}.get(test["status"], "failed")
        return status, test["finished_at"]

    def _profile_response(self, conn, snapshot: ProviderSnapshot | None) -> dict:
        profile = None
        status = None
        if snapshot is not None:
            credential = self._check_credential(conn, snapshot)
            test = self._latest_test(conn, snapshot, credential["generation"])
            test_status, tested_at = self._test_status(test)
            profile = {
                **snapshot.model_dump(mode="json", exclude={"provider_snapshot_hash"}),
                "test_status": test_status,
                "tested_at": tested_at,
            }
            status = self._status(snapshot, credential).model_dump(mode="json")
        return {
            "profile": profile,
            "credential_status": status,
            "os_keyring_available": self._broker.os_keyring_available,
        }

    def get_profile(self, job_id: str | None = None) -> dict:
        with self._profile_lock, self.repo.connect() as conn:
            if job_id is None:
                row = self._active_row(conn)
                snapshot = None if row is None else self._snapshot(row)
            else:
                snapshot = self._job_snapshot(conn, job_id, blocked_only=True)
            return self._profile_response(conn, snapshot)

    def update_profile(self, update: ProfileUpdate) -> dict:
        settings = update.model_dump(mode="json", exclude={"expected_revision"})
        origin = origin_of(update.base_url)
        if self._broker.sanitize(settings) != settings:
            raise ApiError(422, "INVALID_SCHEMA", "تنظیمات عمومی نباید حاوی کلید باشد.")
        with self._profile_lock:
            with self.repo.transaction() as conn:
                row = self._active_row(conn)
                current_revision = None if row is None else row["revision"]
                if update.expected_revision != current_revision:
                    raise _stale()
                if row is not None and _from_json(row["settings"]) == settings:
                    return self._profile_response(conn, self._snapshot(row))
                profile_id = str(uuid4()) if row is None else row["profile_id"]
                revision = 1 if current_revision is None else current_revision + 1
                previous = None if row is None else self._credential_row(conn, row["credential_ref"])
                if previous is not None and previous["origin"] == origin:
                    ref = previous["credential_ref"]
                else:
                    ref = str(uuid4())
                    conn.execute(
                        "INSERT INTO credential_refs (credential_ref, origin) VALUES (?, ?)", (ref, origin)
                    )
                conn.execute(
                    """INSERT INTO provider_profiles
                       (profile_id, revision, credential_ref, settings, created_at) VALUES (?, ?, ?, ?, ?)""",
                    (profile_id, revision, ref, _json(settings), _now()),
                )
                conn.execute(
                    """INSERT INTO active_provider_profile (singleton, profile_id, revision) VALUES (1, ?, ?)
                       ON CONFLICT(singleton) DO UPDATE SET profile_id=excluded.profile_id, revision=excluded.revision""",
                    (profile_id, revision),
                )
                snapshot = self._snapshot(self._profile_row(conn, profile_id, revision))
            with self.repo.connect() as conn:
                return self._profile_response(conn, snapshot)

    @staticmethod
    def _get_job_row(conn, job_id: str):
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ApiError(404, "JOB_NOT_FOUND", "شناسهٔ اجرا یافت نشد.")
        return row

    def _job_snapshot(self, conn, job_id: str, *, blocked_only: bool) -> ProviderSnapshot | None:
        row = self._get_job_row(conn, job_id)
        if blocked_only and row["job_status"] != "needs_configuration":
            raise _invalid_job()
        value = _from_json(row["llm_profile"])
        return None if value is None else ProviderSnapshot.model_validate(value)

    def _mutation_target(self, conn, target: CredentialMutationTarget):
        if target.target_job_id is None:
            row = self._active_row(conn)
            snapshot = None if row is None else self._snapshot(row)
        else:
            snapshot = self._job_snapshot(conn, str(target.target_job_id), blocked_only=True)
        if (
            snapshot is None or snapshot.profile_id != str(target.profile_id)
            or snapshot.revision != target.profile_revision
        ):
            raise _stale()
        credential = self._check_credential(conn, snapshot)
        if credential["generation"] != target.expected_credential_generation:
            raise _stale()
        return snapshot, credential

    def mutate_credential(self, target: CredentialMutationTarget) -> dict:
        with self._profile_lock:
            with self.repo.connect() as conn:
                snapshot, current = self._mutation_target(conn, target)
            ref = current["credential_ref"]
            origin = current["origin"]
            old_mode = current["storage_mode"]
            old_key = self._read_key(current)
            if isinstance(target, SecretSubmission):
                key = target.api_key.get_secret_value()
                mode = target.storage_mode
                if self._broker.is_internal_token(key):
                    raise ApiError(422, "INVALID_SCHEMA", "کلید API معتبر وارد کنید.")
            else:
                key, mode = None, None
            try:
                self._broker.replace(ref, origin, old_mode, mode, key)
            except ApiError:
                if old_key is not None and old_mode == "os_keyring":
                    self._broker.replace(ref, origin, mode, old_mode, old_key)
                raise
            try:
                with self.repo.transaction() as conn:
                    snapshot, credential = self._mutation_target(conn, target)
                    conn.execute(
                        "UPDATE credential_refs SET storage_mode=?, generation=generation+1 WHERE credential_ref=?",
                        (mode, ref),
                    )
                    updated = self._credential_row(conn, ref)
            except Exception:
                self._broker.replace(ref, origin, mode, old_mode, old_key)
                raise
            return self._status(snapshot, updated).model_dump(mode="json")

    def profile_snapshot(self, profile_id, revision: int, *, require_ready=True) -> dict:
        with self._profile_lock, self.repo.connect() as conn:
            row = self._profile_row(conn, str(profile_id), revision)
            if row is None:
                raise _config_required()
            snapshot = self._snapshot(row)
            if require_ready:
                self._ready_key(conn, snapshot)
            return snapshot.model_dump(mode="json")

    def _ready_key(self, conn, snapshot: ProviderSnapshot):
        credential = self._check_credential(conn, snapshot)
        key = self._read_key(credential)
        test = self._latest_test(conn, snapshot, credential["generation"])
        if key is None or test is None or test["status"] != "succeeded":
            raise _config_required()
        return key, credential["generation"]

    def require_ready(self, snapshot: dict | None, target_job_id: str | None = None) -> None:
        if snapshot is None:
            raise _config_required()
        with self._profile_lock, self.repo.connect() as conn:
            if target_job_id is not None:
                expected = self._job_snapshot(conn, target_job_id, blocked_only=True)
                if expected is None or expected.model_dump(mode="json") != snapshot:
                    raise _stale()
            self._ready_key(conn, ProviderSnapshot.model_validate(snapshot))

    def resume_check(self, job: dict) -> None:
        role = next((name for name, phase in _ROLE_PHASES.items() if job["phase"] == phase), None)
        policy = job["policy"]
        if (
            self.count_calls(job["job_id"]) >= policy.get("max_llm_calls", 6)
            or role is not None
            and self.count_calls(job["job_id"], role) >= policy.get("max_llm_calls_per_role", 2)
        ):
            detail = error_detail("LLM_BUDGET_EXHAUSTED")
            raise ApiError(422, detail["error_code"], detail["message_fa"])
        self.require_ready(job["llm_profile"], target_job_id=job["job_id"])

    def count_calls(self, job_id, role: str | None = None) -> int:
        with self.repo.connect() as conn:
            query = "SELECT COUNT(*) FROM llm_calls WHERE job_id=?"
            params: tuple = (str(job_id),)
            if role is not None:
                query += " AND role=?"
                params += (role,)
            return conn.execute(query, params).fetchone()[0]

    def _reserve(
        self, conn, *, job_id: str | None, role: str | None, purpose: str,
        invocation_id: str | None, test_key: str | None, request_hash: str,
        attempt_no: int, snapshot: ProviderSnapshot, generation: int, data: dict, prepared=None,
    ) -> str:
        call_id = str(uuid4())
        conn.execute(
            """INSERT INTO llm_calls (
               call_id, job_id, role, purpose, invocation_id, test_idempotency_key,
               request_hash, attempt_no, provider_snapshot, profile_id, profile_revision,
               credential_generation, prompt_version, input, status, started_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)""",
            (
                call_id, job_id, role, purpose, invocation_id, test_key,
                request_hash, attempt_no, _json(snapshot.model_dump(mode="json")), snapshot.profile_id,
                snapshot.revision, generation,
                (prepared.provenance.get("prompt_version") if prepared is not None else None)
                or {"v2": "2.0", "v2.1": "2.1", "v3": "3.0"}.get(data["schema_id"].partition(".")[2], PROMPT_VERSION),
                _json(data), _now(),
            ),
        )
        if prepared is not None:
            metadata = {"provenance": prepared.provenance}
            metadata.update({key: prepared.provenance[key] for key in (
                "report_generation_policy_version", "report_generation_policy_hash", "effective_max_output_tokens",
                "prompt_name", "prompt_version", "profile_id", "profile_revision") if key in prepared.provenance})
            conn.execute("UPDATE llm_calls SET metadata=? WHERE call_id=?", (_json(metadata), call_id))
        return call_id

    @staticmethod
    def _result(row) -> dict:
        if row["status"] == "reserved":
            raise ApiError(409, "LLM_INVOCATION_PENDING", "درخواست قبلی هنوز در حال اجراست.", retryable=True)
        return {
            "call_id": row["call_id"],
            "status": row["status"],
            "output": _from_json(row["output"]),
            "error": _from_json(row["error"]),
            "metadata": _from_json(row["metadata"]),
            "previous_output": row["previous_output"],
        }

    def _call(self, call_id: str, snapshot: ProviderSnapshot, key: str, schema_id: str, context, prepared=None) -> dict:
        sanitize = lambda value: self._broker.sanitize(value, extra_key=key)
        try:
            outcome = call_provider(
                snapshot, key, schema_id, context, transport=self._transport, sanitize=sanitize, prepared=prepared
            )
        except Exception:
            # Unknown provider outcome after reserving a potentially billable request.
            outcome = {
                "status": "outcome_unknown", "output": None, "metadata": None,
                "error": error_detail("LLM_OUTCOME_UNKNOWN"), "previous_output": None,
            }
        with self.repo.transaction() as conn:
            reserved_metadata = _from_json(conn.execute("SELECT metadata FROM llm_calls WHERE call_id=?", (call_id,)).fetchone()[0]) or {}
            metadata = {**reserved_metadata, **(outcome["metadata"] or {})}
            conn.execute(
                """UPDATE llm_calls SET status=?, output=?, previous_output=?, error=?, metadata=?, finished_at=?
                   WHERE call_id=? AND status='reserved'""",
                (
                    outcome["status"], _json(outcome["output"]), outcome["previous_output"],
                    _json(outcome["error"]), _json(metadata), _now(), call_id,
                ),
            )
            row = conn.execute("SELECT * FROM llm_calls WHERE call_id=?", (call_id,)).fetchone()
            return self._result(row)

    @staticmethod
    def _test_result(row) -> dict:
        result = LLMGateway._result(row)
        return {
            "test_status": "passed" if result["status"] == "succeeded" else "failed",
            "tested_at": row["finished_at"],
            "metadata": result["metadata"],
            "error": result["error"],
        }

    def test_profile(self, target: CredentialMutationTarget, idempotency_key: str) -> dict:
        if not idempotency_key or len(idempotency_key) > 128 or self._broker.sanitize(idempotency_key) != idempotency_key:
            raise ApiError(422, "INVALID_SCHEMA", "شناسهٔ یکتای آزمون اتصال لازم است.")
        request_hash = _hash(target.model_dump(mode="json"))
        # A caller-controlled idempotency header does not need to be stored verbatim.
        test_key = _hash(idempotency_key)
        with self._profile_lock, self.repo.transaction() as conn:
            snapshot, credential = self._mutation_target(conn, target)
            previous = conn.execute(
                "SELECT * FROM llm_calls WHERE test_idempotency_key=?", (test_key,)
            ).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise _idempotency_conflict()
                return self._test_result(previous)
            latest = self._latest_test(conn, snapshot, credential["generation"])
            if latest is not None:
                _check_retry_after(latest["error"])
            key = self._read_key(credential)
            if key is None:
                raise _config_required()
            prepared = prepare_provider_request(snapshot, "connection_test.v1", None)
            call_id = self._reserve(
                conn, job_id=None, role=None, purpose="connection_test", invocation_id=None,
                test_key=test_key, request_hash=request_hash, attempt_no=1, snapshot=snapshot,
                generation=credential["generation"], data={"schema_id": "connection_test.v1"}, prepared=prepared,
            )
        self._call(call_id, snapshot, key, "connection_test.v1", None, prepared)
        with self.repo.connect() as conn:
            return self._test_result(conn.execute("SELECT * FROM llm_calls WHERE call_id=?", (call_id,)).fetchone())

    def invoke(self, invocation: Invocation) -> dict:
        role = invocation.role
        narrative_v3 = invocation.schema_id in {"explainer.v3", "narrative_review.v1"}
        explanation_v2 = invocation.schema_id == "explainer.v2"
        schema_v2 = not explanation_v2 and invocation.schema_id.endswith((".v2", ".v2.1"))
        if narrative_v3:
            from p_mc_optimizer.llm.narrative_models import WriterContextV3, reviewer_context_model
            expected_role, context_model = (("explainer", WriterContextV3) if invocation.schema_id == "explainer.v3"
                                            else ("report_reviewer", reviewer_context_model(invocation.context)))
        elif explanation_v2:
            from p_mc_optimizer.llm.explanation_v2 import ExplainerContextV2

            expected_role, context_model = "explainer", ExplainerContextV2
        elif schema_v2:
            from p_mc_optimizer.llm.research_v2 import SCHEMAS

            expected_role, context_model, _ = SCHEMAS[invocation.schema_id]
        else:
            expected_role = role if invocation.schema_id == SCHEMA_IDS.get(role) else None
            context_model = ROLE_CONTEXTS.get(role)
        if expected_role != role:
            raise ApiError(422, "INVALID_SCHEMA", "نقش و شناسهٔ schema یکسان نیستند.")
        try:
            context = context_model.model_validate(invocation.context)
            _validate_context_fields(invocation.context)
            if role == "explainer" and not narrative_v3 and context.report.get("decision_summary") is not None:
                ExplainerDecisionSummary.model_validate(context.report["decision_summary"])
            data = invocation.model_dump(mode="json")
            request_hash = _hash(data)
        except (ValidationError, ValueError, RecursionError):
            raise ApiError(422, "INVALID_SCHEMA", "دادهٔ نقش با قرارداد سازگار نیست.") from None
        if self._broker.sanitize(data) != data:
            raise ApiError(422, "INVALID_SCHEMA", "دادهٔ نقش نباید حاوی کلید یا توکن باشد.")
        job_id = str(invocation.job_id)
        invocation_id = str(invocation.invocation_id)
        with self._profile_lock, self.repo.transaction() as conn:
            previous = conn.execute(
                "SELECT * FROM llm_calls WHERE invocation_id=?", (invocation_id,)
            ).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise _idempotency_conflict()
                if narrative_v3:
                    # Context equality alone does not bind server policy or wire caps.
                    job = self._get_job_row(conn, job_id)
                    try:
                        version = self._narrative_policy_version(job)
                        frozen_snapshot = self._job_snapshot(conn, job_id, blocked_only=False)
                        if frozen_snapshot is None:
                            raise ValueError("Missing frozen provider snapshot")
                        prepared = prepare_provider_request(frozen_snapshot, invocation.schema_id, context,
                                                            report_generation_policy_version=version)
                        prior = (_from_json(previous["metadata"]) or {}).get("provenance") or {}
                        if (prior.get("request_body_sha256") != prepared.provenance["request_body_sha256"]
                                or prior.get("report_generation_policy_version", "1.0") != version
                                or prior.get("report_generation_policy_hash", prepared.provenance["report_generation_policy_hash"] if version == "1.0" else None)
                                != prepared.provenance["report_generation_policy_hash"]):
                            raise ValueError("Frozen request policy/body changed")
                    except (ValueError, ApiError):
                        raise _idempotency_conflict() from None
                return self._result(previous)
            job = self._get_job_row(conn, job_id)
            if job["job_status"] != "running" or job["phase"] != _ROLE_PHASES[role] or job["cancel_requested"]:
                raise _invalid_job()
            policy = _from_json(job["policy"])
            policy_version = policy["policy_version"]
            if role in {"explainer", "report_reviewer"}:
                contract = _from_json(job["workflow"]).get("execution_contracts", {}).get("explanation")
                expected_schema = (("explainer.v3" if role == "explainer" else "narrative_review.v1")
                                   if contract == "grounded_narrative_v3" else
                                   "explainer.v2" if contract == "evidence_bound_v2" else "explainer.v1")
                if invocation.schema_id != expected_schema:
                    raise _invalid_job()
            version_two = policy_version in {"2.0", "2.1"}
            if role in {"advisor", "research_planner"}:
                suffix = {"1.0": "v1", "2.0": "v2", "2.1": "v2.1"}[policy_version]
                if invocation.schema_id != f"{role}.{suffix}":
                    raise _invalid_job()
            if narrative_v3:
                self._validate_narrative_context(job, invocation, context)
            else:
                self._validate_job_policy(policy, role, context)
            if narrative_v3:
                pass
            elif schema_v2:
                self._validate_v2_context(job, role, context)
            else:
                self._validate_job_context(conn, job, role, context)
            charged_calls = conn.execute(
                "SELECT role, error FROM llm_calls WHERE job_id=? ORDER BY started_at, rowid", (job_id,)
            ).fetchall()
            role_calls = [call for call in charged_calls if call["role"] == role]
            if (
                len(charged_calls) >= min(8 if version_two else 6, policy.get("max_llm_calls", 6))
                or len(role_calls) >= min(2, policy.get("max_llm_calls_per_role", 2))
            ):
                detail = error_detail("LLM_BUDGET_EXHAUSTED")
                raise ApiError(422, detail["error_code"], detail["message_fa"])
            if narrative_v3 and role == "explainer":
                reviewer_calls = sum(call["role"] == "report_reviewer" for call in charged_calls)
                if (min(8 if version_two else 6, policy.get("max_llm_calls", 6)) - len(charged_calls) < 2
                        or reviewer_calls >= min(2, policy.get("max_llm_calls_per_role", 2))):
                    detail = error_detail("LLM_BUDGET_EXHAUSTED")
                    raise ApiError(422, detail["error_code"], detail["message_fa"])
            if role_calls:
                _check_retry_after(role_calls[-1]["error"])
            snapshot = self._job_snapshot(conn, job_id, blocked_only=False)
            if snapshot is None:
                raise _config_required()
            key, generation = self._ready_key(conn, snapshot)
            if self._broker.sanitize(data, extra_key=key) != data:
                raise ApiError(422, "INVALID_SCHEMA", "دادهٔ نقش نباید حاوی کلید یا توکن باشد.")
            producer = _from_json(job["workflow"]).get("runtime:invocation_producers", {}).get(invocation_id)
            prepared = prepare_provider_request(snapshot, invocation.schema_id, context, producer=producer,
                                                report_generation_policy_version=self._narrative_policy_version(job) if narrative_v3 else "1.0")
            call_id = self._reserve(
                conn, job_id=job_id, role=role, purpose="job_role",
                invocation_id=invocation_id, test_key=None, request_hash=request_hash,
                attempt_no=len(role_calls) + 1, snapshot=snapshot, generation=generation, data=data, prepared=prepared,
            )
        with self.repo.transaction() as conn:
            if self._get_job_row(conn, job_id)["cancel_requested"]:
                conn.execute("DELETE FROM llm_calls WHERE call_id=? AND status='reserved'", (call_id,))
                cancelled = True
            else:
                cancelled = False
        if cancelled:
            raise _invalid_job()
        return self._call(call_id, snapshot, key, invocation.schema_id, context, prepared)

    @staticmethod
    def _narrative_policy_version(job):
        from p_mc_optimizer.llm.narrative_policy import freeze_narrative_policy
        contracts = _from_json(job["workflow"]).get("execution_contracts") or {}
        version = contracts.get("report_generation_policy_version", "1.0")
        try:
            frozen = freeze_narrative_policy(version)
        except ValueError:
            raise ApiError(422, "INVALID_SCHEMA", "نسخهٔ سیاست گزارش معتبر نیست.") from None
        if (contracts.get("report_generation_policy", frozen if version == "1.0" else None) != frozen
                or contracts.get("evidence_builder_version", "1.0") != frozen["evidence_builder_version"]
                or contracts.get("formatter_version", "1.0") != frozen["formatter_version"]):
            raise ApiError(422, "INVALID_SCHEMA", "نسخه‌های سیاست و نمایش گزارش هم‌خوان نیستند.")
        return version

    @staticmethod
    def _validate_narrative_context(job, invocation, context):
        from p_mc_optimizer.llm.narrative_policy import freeze_narrative_policy
        workflow = _from_json(job["workflow"])
        journal = workflow.get("narrative_v3_journal") or {}
        active = journal.get("active_request") or {}
        actual = context.model_dump(mode="json")
        version = LLMGateway._narrative_policy_version(job)
        frozen = freeze_narrative_policy(version)
        if (workflow.get("execution_contracts", {}).get("explanation") != "grounded_narrative_v3"
                or not _from_json(job["policy"]).get("explain_via_llm")
                or active.get("invocation_id") != str(invocation.invocation_id)
                or active.get("role") != invocation.role
                or active.get("schema_id") != invocation.schema_id
                or active.get("context") != actual
                or active.get("request_hash") != actual["request_hash"]
                or active.get("status") != "ready"
                or actual["evidence"]["job_id"] != job["job_id"]
                or actual["evidence"] != journal.get("evidence")
                or actual["context_hash"] != journal.get("context_hash")
                or actual["evidence"]["source_core_sha256"] != journal.get("source_core_sha256")
                or actual["evidence"]["evidence_policy_version"] != frozen["evidence_policy_version"]
                or (invocation.role == "report_reviewer" and actual["schema_version"] != frozen.get("reviewer_context_version", "1.0"))
                or journal.get("report_generation_policy_version", "1.0") != version
                or journal.get("report_generation_policy", frozen if version == "1.0" else None) != frozen
                or journal.get("report_generation_policy_hash", frozen["report_generation_policy_hash"] if version == "1.0" else None) != frozen["report_generation_policy_hash"]
                or actual["prompt_bundle_sha256"] != _hash(prompt_bundle_manifest())
                or journal.get("prompt_bundle_sha256", actual["prompt_bundle_sha256"]) != actual["prompt_bundle_sha256"]):
            raise ApiError(422, "INVALID_SCHEMA", "درخواست گزارش با گام ثبت‌شدهٔ سرور یکسان نیست.")
        without_hash = {key: value for key, value in actual.items() if key != "request_hash"}
        if _hash(without_hash) != actual["request_hash"]:
            raise ApiError(422, "INVALID_SCHEMA", "هش درخواست گزارش معتبر نیست.")
        if len(_json(actual).encode("utf-8")) > 196608:
            raise ApiError(422, "CONTEXT_TOO_LARGE", "زمینهٔ گزارش بیش از حد مجاز است.")

    @staticmethod
    def _validate_job_policy(policy: dict, role: str, context) -> None:
        selection = policy.get("selection_policy")
        if selection not in _LLM_POLICIES:
            raise _invalid_job()
        if role == "research_planner" and selection not in {"web_research_then_pilot", "evidence_research_then_pilot"}:
            raise _invalid_job()
        if role == "explainer":
            if not policy.get("explain_via_llm"):
                raise _invalid_job()
            if set(context.report) - _REPORT_FIELDS:
                raise ApiError(422, "INVALID_SCHEMA", "خلاصهٔ توضیح‌دهنده دارای فیلد نامجاز است.")
        if role == "advisor" and context.candidate_count != (1 if selection == "llm_only" else 3):
            raise ApiError(422, "INVALID_SCHEMA", "تعداد کاندیدا با سیاست اجرا یکسان نیست.")

    @staticmethod
    def _validate_v2_context(job, role, context):
        workflow = _from_json(job["workflow"])
        expected = workflow.get(f"role_context:{role}")
        actual = context.model_dump(mode="json", exclude={"repair"})
        if expected is None or actual != expected:
            raise ApiError(422, "INVALID_SCHEMA", "زمینهٔ پژوهش با snapshot این اجرا یکسان نیست.")

    @staticmethod
    def _validate_job_context(conn, job, role: str, context) -> None:
        if role == "formulator":
            job_input = _from_json(job["input"])
            if job_input["kind"] != "natural_language" or job["completion_input"] or job["problem_revision_id"]:
                raise _invalid_job()
            known = {"input:text": job_input["text"]}
            known.update({
                f"answer:{item['question_id']}": item["answer"] for item in _from_json(job["answers"])
            })
            if {source.source_ref: source.text for source in context.sources} != known:
                raise ApiError(422, "INVALID_SCHEMA", "متن صورت‌بند با منابع همین اجرا یکسان نیست.")
        elif role == "advisor":
            from p_mc_optimizer.solvers.registry import solver_cards

            revision = conn.execute(
                "SELECT spec FROM problem_revisions WHERE problem_revision_id=?", (job["problem_revision_id"],)
            ).fetchone()
            if revision is None:
                raise _invalid_job()
            expected = _from_json(revision["spec"])
            expected.pop("source_map", None)
            if context.problem.model_dump(mode="json") != expected:
                raise ApiError(422, "INVALID_SCHEMA", "مدل زمینه با نسخهٔ ثابت اجرا یکسان نیست.")
            registry = {card["algorithm_id"]: card for card in solver_cards()}
            if not context.solver_cards or any(
                card != registry.get(card.get("algorithm_id")) for card in context.solver_cards
            ):
                raise ApiError(422, "INVALID_SCHEMA", "کارت‌های روش با سبد ثابت یکسان نیستند.")
        elif role == "explainer":
            expected = _from_json(job["workflow"]).get("role_context:explainer")
            actual = context.model_dump(mode="json", exclude={"repair"})
            if expected is None or _json(actual) != _json(expected):
                raise ApiError(422, "INVALID_SCHEMA", "زمینهٔ توضیح با snapshot این اجرا یکسان نیست.")
            if "decision_summary" not in context.report:
                raise ApiError(422, "INVALID_SCHEMA", "فیلد خلاصهٔ تصمیم در زمینهٔ توضیح الزامی است.")
            if context.selection_made != (context.report.get("selected_algorithm_id") is not None):
                raise ApiError(422, "INVALID_SCHEMA", "وضعیت انتخاب با زمینهٔ توضیح این اجرا یکسان نیست.")
            has_decision = context.report["decision_summary"] is not None
            if has_decision != ("report:decision" in context.evidence_refs) or (context.selection_made and not has_decision):
                raise ApiError(422, "INVALID_SCHEMA", "ارجاع تصمیم با شواهد زمینهٔ توضیح یکسان نیست.")
