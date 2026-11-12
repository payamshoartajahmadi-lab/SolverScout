"""Local job submission, optimistic mutations, and public job projections."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.domain.models import CreateJob, ExecutionPolicy, ProblemInput
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.persistence.repository import invalid_state, json_text, utc_now, uuid4_text
from p_mc_optimizer.research.contracts import ExecutionPolicyV2
from p_mc_optimizer.research.contracts_v21 import ExecutionPolicyV21

Version = Annotated[int, Field(ge=1)]
ShortId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
AnswerText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]
INTERNAL_JOB_FIELDS = {"workflow", "completion_input", "answers", "owner_id", "lease_expires_at", "input", "llm_profile"}


class JobVersion(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    expected_job_version: Version


class DraftVersion(JobVersion):
    expected_draft_version: Version


class TextualAnswer(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    question_id: ShortId
    answer: AnswerText


class TextualAnswers(DraftVersion):
    mode: Literal["textual_answers"]
    answers: Annotated[list[TextualAnswer], Field(min_length=1, max_length=80)]


class StructuredCompletion(DraftVersion):
    mode: Literal["structured_completion"]
    problem: ProblemInput


AnswersInput = Annotated[TextualAnswers | StructuredCompletion, Field(discriminator="mode")]


def public_job(job: dict) -> dict:
    public = {key: value for key, value in job.items() if key not in INTERNAL_JOB_FIELDS}
    if job["policy"]["policy_version"] == "2.1":
        public["source_sync_run_id"] = job["workflow"].get("source_sync_run_id")
    return public


def not_configured(*_) -> None:
    raise ApiError(422, "PROVIDER_CONFIGURATION_REQUIRED", "تنظیمات و کلید API را ثبت و اتصال را آزمایش کنید.")


class ApplicationService:
    def __init__(self, repo: Repository, *, profile_snapshot=not_configured, resume_check=not_configured, freeze_v2=None):
        self.repo = repo
        self.profile_snapshot = profile_snapshot
        self.resume_check = resume_check
        self.freeze_v2 = freeze_v2

    def create_job(self, request: CreateJob, idempotency_key: str) -> dict:
        if not idempotency_key or len(idempotency_key) > 200 or not all(
            33 <= ord(character) <= 126 for character in idempotency_key
        ):
            raise ApiError(422, "INVALID_SCHEMA", "سربرگ Idempotency-Key معتبر الزامی است.")
        idempotency_key = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
        payload = request.model_dump(mode="json")
        request_hash = hashlib.sha256(json_text(payload).encode("utf-8")).hexdigest()
        policy_type = {"1.0": ExecutionPolicy, "2.0": ExecutionPolicyV2, "2.1": ExecutionPolicyV21}[
            request.policy.policy_version
        ]
        v2 = policy_type is not ExecutionPolicy
        policy = policy_type.from_input(request.policy).model_dump(mode="json")
        with self.repo.connect() as connection:
            connection.execute("BEGIN")
            existing = self._idempotent_job(connection, idempotency_key, request_hash)
            if existing is not None:
                return existing
        profile_ref = payload["llm_profile"]
        frozen = None
        if v2:
            if self.freeze_v2 is None:
                raise ApiError(422, "CATALOG_QUALIFICATION_REQUIRED", "کاتالوگ این اجرا آماده نیست.")
            frozen = self.freeze_v2(request)
        snapshot = (
            self.profile_snapshot(profile_ref["profile_id"], profile_ref["revision"])
            if profile_ref is not None and not v2 else None
        )
        if v2:
            snapshot = frozen.pop("llm_profile", None)
        request_input = payload["input"]
        revision_id = request_input.get("problem_revision_id")
        job_id = uuid4_text()
        timestamp = utc_now()
        with self.repo.transaction() as connection:
            existing = self._idempotent_job(connection, idempotency_key, request_hash)
            if existing is not None:
                return existing
            from p_mc_optimizer.runtime_provenance import get_runtime, save_runtime

            creator = get_runtime()
            save_runtime(self.repo.data_dir)
            workflow = {
                "execution_contracts": {"explanation": "grounded_narrative_v3", "writer_schema": "explainer.v3",
                                        "reviewer_schema": "narrative_review.v1", "evidence_builder_version": "1.0",
                                        "renderer_version": "3.0.0", "formatter_version": "1.0"},
                "runtime:creator": creator,
            }
            if revision_id is not None and connection.execute(
                "SELECT 1 FROM problem_revisions WHERE problem_revision_id=?", (revision_id,)
            ).fetchone() is None:
                raise ApiError(404, "PROBLEM_REVISION_NOT_FOUND", "نسخهٔ ثبت‌شدهٔ مسئله پیدا نشد.")
            connection.execute(
                """INSERT INTO jobs (job_id,input,policy,llm_profile,problem_revision_id,job_status,
                    phase,created_at,updated_at,workflow) VALUES (?,?,?,?,?,'queued','intake',?,?,?)""",
                (job_id, json_text(request_input), json_text(policy),
                 json_text(snapshot) if snapshot is not None else None, revision_id, timestamp, timestamp,
                 json_text(workflow)),
            )
            connection.execute(
                """INSERT INTO idempotency_records VALUES ('create_job',?,?,?,?)""",
                (idempotency_key, request_hash, job_id, timestamp),
            )
            if frozen is not None:
                connection.execute("INSERT INTO job_v2_snapshots VALUES (?,?)", (job_id, json_text(frozen)))
            self.repo.write_event(connection, job_id, "job_queued")
            return self.repo.job_snapshot(connection, self.repo.job_row(connection, job_id))

    def _idempotent_job(self, connection, key, request_hash) -> dict | None:
        existing = connection.execute(
            "SELECT request_hash, resource_id FROM idempotency_records "
            "WHERE operation_scope='create_job' AND idempotency_key=?", (key,),
        ).fetchone()
        if existing is None:
            return None
        if existing["request_hash"] != request_hash:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "این کلید ارسال برای درخواست دیگری ثبت شده است.")
        return self.repo.job_snapshot(connection, self.repo.job_row(connection, existing["resource_id"]))

    def answer_job(self, job_id: str, request: AnswersInput) -> dict:
        problem = None
        if isinstance(request, StructuredCompletion):
            problem = request.problem.model_dump(mode="json")
        with self.repo.transaction() as connection:
            job = self.repo.job_row(connection, job_id)
            self.repo.check_version(job, request.expected_job_version)
            if job["job_status"] != "needs_input" or job["problem_revision_id"] is not None:
                raise invalid_state()
            draft_row = connection.execute(
                "SELECT * FROM problem_drafts WHERE job_id=?", (job_id,),
            ).fetchone()
            if draft_row is None:
                raise invalid_state()
            if draft_row["draft_version"] != request.expected_draft_version:
                raise ApiError(409, "STALE_VERSION", "پیش‌نویس تغییر کرده است. داده‌ها را بازخوانی کنید.")
            draft = json.loads(draft_row["payload"])
            workflow = json.loads(job["workflow"])
            fields = {"job_status": "queued", "blocked_reason": None, "error": None}
            if isinstance(request, TextualAnswers):
                submitted = {item.question_id for item in request.answers}
                answers = json.loads(job["answers"])
                previously_answered = {item["question_id"] for item in answers}
                allowed = {str(question["question_id"]) for question in draft.get("ambiguities", [])}
                if len(submitted) != len(request.answers) or not submitted <= allowed - previously_answered:
                    raise ApiError(422, "INVALID_SCHEMA", "فقط به پرسش‌های باز همین پیش‌نویس پاسخ دهید.")
                policy = json.loads(job["policy"])
                calls = connection.execute(
                    "SELECT COUNT(*) AS total, "
                    "COALESCE(SUM(role='formulator'),0) AS for_role FROM llm_calls WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if calls["total"] >= policy["max_llm_calls"] or calls["for_role"] >= policy["max_llm_calls_per_role"]:
                    raise ApiError(422, "LLM_BUDGET_EXHAUSTED", "سهم صورت‌بندی پایان یافته است. فرم ساخت‌یافته را تکمیل کنید.")
                fields["answers"] = [*answers, *(item.model_dump() for item in request.answers)]
                fields["phase"] = "formulating"
                workflow["clarification_count"] = workflow.get("clarification_count", 0) + 1
            else:
                fields["completion_input"] = problem
                fields["phase"] = "validating_input"
            fields["workflow"] = workflow
            draft["draft_version"] = draft_row["draft_version"] + 1
            connection.execute(
                "UPDATE problem_drafts SET draft_version=?, payload=? WHERE job_id=?",
                (draft["draft_version"], json_text(draft), job_id),
            )
            self.repo.write_job(connection, job_id, fields)
            self.repo.write_event(connection, job_id, "answers_submitted", {
                "mode": request.mode, "draft_version": draft["draft_version"],
            })
            return self.repo.job_snapshot(connection, self.repo.job_row(connection, job_id))

    def resume_job(self, job_id: str, expected_version: int) -> dict:
        job = self.repo.get_job(job_id)
        self.repo.check_version(job, expected_version)
        if job["job_status"] != "needs_configuration":
            raise invalid_state()
        self.resume_check(job)
        with self.repo.transaction() as connection:
            row = self.repo.job_row(connection, job_id)
            self.repo.check_version(row, expected_version)
            if row["job_status"] != "needs_configuration":
                raise invalid_state()
            workflow = json.loads(row["workflow"])
            workflow["resume_count"] = workflow.get("resume_count", 0) + 1
            self.repo.write_job(connection, job_id, {
                "job_status": "queued", "blocked_reason": None, "error": None, "workflow": workflow,
            })
            self.repo.write_event(connection, job_id, "job_resumed", {"phase": row["phase"]})
            return self.repo.job_snapshot(connection, self.repo.job_row(connection, job_id))

    def cancel_job(self, job_id: str, expected_version: int) -> tuple[int, dict]:
        with self.repo.transaction() as connection:
            row = self.repo.job_row(connection, job_id)
            if row["job_status"] == "cancelled":
                return 200, self.repo.job_snapshot(connection, row)
            self.repo.check_version(row, expected_version)
            if row["job_status"] in {"completed", "failed"}:
                raise invalid_state()
            if row["cancel_requested"]:
                return 202, self.repo.job_snapshot(connection, row)
            fields = {"cancel_requested": True}
            if row["job_status"] != "running":
                fields.update(
                    job_status="cancelled", termination_reason="cancelled",
                    solution_status="validation_incomplete", explanation_status="not_requested",
                    partial_result=True, owner_id=None, lease_expires_at=None,
                )
            self.repo.write_job(connection, job_id, fields)
            self.repo.write_event(connection, job_id, "cancel_requested")
            if fields.get("job_status") == "cancelled":
                self.repo.write_diagnostic_report(connection, job_id)
                self.repo.write_event(connection, job_id, "job_finished", {"job_status": "cancelled"})
            return 202, self.repo.job_snapshot(connection, self.repo.job_row(connection, job_id))
