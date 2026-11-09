"""Durable local job state, execution ledger, and artifact metadata."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from p_mc_optimizer.application.errors import ApiError

from .schema import BASE_SCHEMA, SCHEMA_VERSION

TERMINAL_JOB = frozenset({"completed", "failed", "cancelled"})
TERMINAL_EXECUTION = frozenset({"completed", "failed", "timed_out", "cancelled", "unknown_outcome"})
UNKNOWN_EXECUTION = {
    "error_code": "UNKNOWN_EXECUTION_OUTCOME",
    "message_fa": "فرایند عددی پیش از ثبت نتیجه متوقف شد. سهم نامعلوم محفوظ است.",
}
JOB_JSON = {"input", "policy", "llm_profile", "error", "workflow", "completion_input", "answers"}
JOB_BOOL = {"partial_result", "cancel_requested"}
JOB_PATCH = {
    "job_status", "phase", "solution_status", "optimality_status", "explanation_status", "selection_status",
    "termination_reason", "partial_result", "cancel_requested", "blocked_reason",
    "error", "workflow",
}
QUOTAS = {
    "preflight": "preflight_evaluations",
    "profile": "profile_evaluations",
    "verification": "verification_evaluations",
    "pilot": "pilot_evaluations",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def json_text(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def as_json(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def uuid4_text(value: str | UUID | None = None) -> str:
    result = uuid4() if value is None else UUID(str(value))
    if result.version != 4:
        raise ValueError("Expected a UUID version 4")
    return str(result)


def missing_job() -> ApiError:
    return ApiError(404, "JOB_NOT_FOUND", "این اجرا پیدا نشد.")


def invalid_state() -> ApiError:
    return ApiError(409, "INVALID_JOB_STATE", "این عملیات در وضعیت فعلی اجرا مجاز نیست.")


class Repository:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "artifacts").mkdir(exist_ok=True)
        self.path = self.data_dir / "state.sqlite3"
        self._migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _migrate(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version != 0:
                raise RuntimeError(f"Unsupported state.sqlite3 schema version {version}")
            existing = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()
            if existing:
                backup_dir = self.data_dir / "backups"
                backup_dir.mkdir(exist_ok=True)
                backup_path = backup_dir / f"state-v{version}-{uuid4()}.sqlite3"
                with self.connect() as source, sqlite3.connect(backup_path) as backup:
                    source.backup(backup)
            from p_mc_optimizer.llm.storage import LLM_SCHEMA

            statement = ""
            for line in (BASE_SCHEMA + "\n" + LLM_SCHEMA).splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("Incomplete SQL migration")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def job_row(connection, job_id) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (str(job_id),)).fetchone()
        if row is None:
            raise missing_job()
        return row

    @staticmethod
    def decode_job(row) -> dict:
        job = dict(row)
        for key in JOB_JSON:
            if job[key] is not None:
                job[key] = json.loads(job[key])
        for key in JOB_BOOL:
            job[key] = bool(job[key])
        return job

    @staticmethod
    def budget(connection, job_id, total) -> dict:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(known_started),0) AS known_started,
                COALESCE(SUM(reserved_active),0) AS reserved_active,
                COALESCE(SUM(charged_unknown),0) AS charged_unknown,
                COALESCE(SUM(unused),0) AS unused,
                COALESCE(SUM(evaluation_quota),0) AS allocated
            FROM (
                SELECT known_started, reserved_active, charged_unknown, unused, evaluation_quota
                FROM stage_executions WHERE job_id=?
                UNION ALL
                SELECT a.known_started, a.reserved_active, a.charged_unknown, a.unused, a.evaluation_quota
                FROM trial_attempts a JOIN trials t ON t.trial_id=a.trial_id WHERE t.job_id=?
            )
            """,
            (job_id, job_id),
        ).fetchone()
        return {
            "max_objective_evaluations": total, **dict(row),
            "unallocated": total - row["allocated"],
        }

    def job_snapshot(self, connection, row) -> dict:
        job = self.decode_job(row)
        job["budget"] = self.budget(connection, job["job_id"], job["policy"]["max_objective_evaluations"])
        return job

    def get_job(self, job_id) -> dict:
        with self.connect() as connection:
            connection.execute("BEGIN")
            return self.job_snapshot(connection, self.job_row(connection, job_id))

    def list_jobs(self, *, limit=100, offset=0) -> list[dict]:
        with self.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, job_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self.job_snapshot(connection, row) for row in rows]

    @staticmethod
    def write_event(connection, job_id, event_type, payload=None) -> int:
        row = connection.execute(
            "UPDATE jobs SET event_sequence=event_sequence+1 WHERE job_id=? RETURNING event_sequence",
            (str(job_id),),
        ).fetchone()
        if row is None:
            raise missing_job()
        sequence = row["event_sequence"]
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?,?)",
            (str(job_id), sequence, event_type, utc_now(), json_text({} if payload is None else payload)),
        )
        return sequence

    def append_event(self, job_id, event_type, payload=None, *, worker_id=None) -> int:
        with self.transaction() as connection:
            self.check_worker(self.job_row(connection, job_id), worker_id)
            return self.write_event(connection, job_id, event_type, payload)

    def list_events(self, job_id, after_sequence=0, limit=500) -> list[dict]:
        with self.connect() as connection:
            self.job_row(connection, job_id)
            rows = connection.execute(
                "SELECT * FROM events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (str(job_id), after_sequence, limit),
            )
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    @staticmethod
    def check_worker(row, worker_id) -> None:
        if worker_id is not None and (
            row["job_status"] != "running" or row["owner_id"] != str(worker_id)
            or row["lease_expires_at"] is None or row["lease_expires_at"] <= utc_now()
        ):
            raise ApiError(409, "WORKER_LEASE_LOST", "مهلت مالکیت اجرای Worker پایان یافته است.")

    @staticmethod
    def check_version(row, expected_version) -> None:
        if row["version"] != expected_version:
            raise ApiError(409, "STALE_VERSION", "وضعیت اجرا تغییر کرده است. داده‌ها را بازخوانی کنید.")

    @staticmethod
    def write_job(connection, job_id, fields) -> None:
        if not fields:
            return
        allowed = JOB_PATCH | {"owner_id", "lease_expires_at", "problem_revision_id", "completion_input", "answers"}
        if not fields.keys() <= allowed:
            raise ValueError("Unsupported job update")
        values = [json_text(as_json(value)) if key in JOB_JSON and value is not None else value
                  for key, value in fields.items()]
        setters = ", ".join(f"{key}=?" for key in fields)
        connection.execute(
            f"UPDATE jobs SET {setters}, updated_at=?, version=version+1 WHERE job_id=?",
            (*values, utc_now(), str(job_id)),
        )

    def update_job(self, job_id, event_type=None, event_payload=None, *, worker_id=None, **fields) -> dict:
        if not fields.keys() <= JOB_PATCH:
            raise ValueError("Unsupported job update")
        with self.transaction() as connection:
            row = self.job_row(connection, job_id)
            self.check_worker(row, worker_id)
            if row["job_status"] in TERMINAL_JOB:
                raise invalid_state()
            if fields.get("job_status", "running") != "running":
                fields.update(owner_id=None, lease_expires_at=None)
            self.write_job(connection, job_id, fields)
            if event_type is not None:
                self.write_event(connection, job_id, event_type, event_payload)
            return self.job_snapshot(connection, self.job_row(connection, job_id))

    def put_workflow(self, job_id, key, value, *, worker_id=None) -> None:
        with self.transaction() as connection:
            row = self.job_row(connection, job_id)
            self.check_worker(row, worker_id)
            workflow = json.loads(row["workflow"])
            workflow[key] = as_json(value)
            connection.execute("UPDATE jobs SET workflow=? WHERE job_id=?", (json_text(workflow), str(job_id)))

    def claim_job(self, worker_id, lease_seconds=30, *, job_id=None) -> dict | None:
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE job_status='running' LIMIT 1").fetchone():
                return None
            query = "SELECT job_id FROM jobs WHERE job_status='queued' AND cancel_requested=0"
            parameters = ()
            if job_id is not None:
                query += " AND job_id=?"
                parameters = (str(job_id),)
            row = connection.execute(query + " ORDER BY created_at, job_id LIMIT 1", parameters).fetchone()
            if row is None:
                return None
            job_id = row["job_id"]
            self.write_job(connection, job_id, {
                "job_status": "running", "owner_id": str(worker_id),
                "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(),
            })
            self.write_event(connection, job_id, "job_claimed")
            return self.job_snapshot(connection, self.job_row(connection, job_id))

    def renew_lease(self, job_id, worker_id, lease_seconds=30) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET lease_expires_at=? WHERE job_id=? AND owner_id=? "
                "AND job_status='running' AND lease_expires_at>?",
                ((datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(),
                 str(job_id), str(worker_id), utc_now()),
            )
            return cursor.rowcount == 1

    def recover_expired_jobs(self, *, force=False) -> list[str]:
        """Requeue recoverable jobs and permanently charge unfinished reservations.

        Force is for launcher startup after acquiring exclusive instance ownership.
        """
        recovered = []
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE job_status='running' AND (? OR lease_expires_at<=?)",
                (force, utc_now()),
            ).fetchall()
            for row in rows:
                job_id = row["job_id"]
                unfinished = [item for item in self.execution_rows(connection, job_id)
                              if item["execution_status"] == "running"]
                for item in unfinished:
                    self._finish(connection, item, {
                        "execution_status": "unknown_outcome", "termination_reason": "execution_error",
                        "started_evaluations": item["known_started"],
                        "history_complete": False, "error": UNKNOWN_EXECUTION,
                    })
                fields = {"owner_id": None, "lease_expires_at": None}
                if row["cancel_requested"]:
                    fields.update(job_status="cancelled", termination_reason="cancelled",
                                  solution_status="validation_incomplete", partial_result=True,
                                  explanation_status="not_requested")
                elif unfinished:
                    fields.update(job_status="failed", termination_reason="execution_error",
                                  solution_status="validation_incomplete", partial_result=True,
                                  explanation_status="not_requested",
                                  error=UNKNOWN_EXECUTION)
                else:
                    fields["job_status"] = "queued"
                self.write_job(connection, job_id, fields)
                if fields["job_status"] in TERMINAL_JOB:
                    self.write_diagnostic_report(connection, job_id)
                self.write_event(connection, job_id, "job_recovered", {
                    "job_status": fields["job_status"],
                    "unknown_execution_ids": [item["execution_id"] for item in unfinished],
                })
                recovered.append(job_id)
        return recovered

    def put_draft(self, job_id, draft, *, worker_id=None) -> dict:
        data = dict(as_json(draft))
        with self.transaction() as connection:
            job = self.job_row(connection, job_id)
            self.check_worker(job, worker_id)
            if job["problem_revision_id"] is not None:
                raise invalid_state()
            existing = connection.execute(
                "SELECT * FROM problem_drafts WHERE job_id=?", (str(job_id),)
            ).fetchone()
            if existing is None:
                data["draft_id"] = uuid4_text(data.get("draft_id"))
                data["draft_version"] = 1
                connection.execute(
                    "INSERT INTO problem_drafts VALUES (?,?,?,?)",
                    (data["draft_id"], str(job_id), data["draft_version"], json_text(data)),
                )
            else:
                data["draft_id"] = existing["draft_id"]
                data["draft_version"] = existing["draft_version"] + 1
                connection.execute(
                    "UPDATE problem_drafts SET draft_version=?, payload=? WHERE job_id=?",
                    (data["draft_version"], json_text(data), str(job_id)),
                )
            self.write_event(connection, job_id, "draft_updated", {
                "draft_id": data["draft_id"], "draft_version": data["draft_version"],
            })
            return data

    def get_problem(self, job_id) -> dict:
        with self.connect() as connection:
            connection.execute("BEGIN")
            job = self.job_row(connection, job_id)
            draft = connection.execute(
                "SELECT payload FROM problem_drafts WHERE job_id=?", (str(job_id),)
            ).fetchone()
            revision = connection.execute(
                "SELECT payload FROM problem_revisions WHERE problem_revision_id=?",
                (job["problem_revision_id"],),
            ).fetchone()
            return {
                "frozen": revision is not None,
                "draft": None if draft is None else json.loads(draft["payload"]),
                "revision": None if revision is None else json.loads(revision["payload"]),
            }

    def freeze_problem(self, job_id, revision, *, worker_id=None) -> dict:
        data = as_json(revision)
        revision_id = uuid4_text(data["problem_revision_id"])
        with self.transaction() as connection:
            job = self.job_row(connection, job_id)
            self.check_worker(job, worker_id)
            if job["problem_revision_id"] is not None:
                existing = connection.execute(
                    "SELECT payload FROM problem_revisions WHERE problem_revision_id=?",
                    (job["problem_revision_id"],),
                ).fetchone()
                if job["problem_revision_id"] == revision_id:
                    return json.loads(existing["payload"])
                raise invalid_state()
            connection.execute(
                "INSERT INTO problem_revisions VALUES (?,?,?,?,?,?,?,?)",
                (revision_id, str(data["problem_id"]), data["revision_no"], json_text(data["spec"]),
                 data["problem_math_hash"], data["evaluation_contract_hash"], json_text(data),
                 data.get("created_at", utc_now())),
            )
            self.write_job(connection, job_id, {"problem_revision_id": revision_id})
            self.write_event(connection, job_id, "problem_frozen", {"problem_revision_id": revision_id})
            return data

    def put_decision(self, job_id, decision, *, worker_id=None) -> dict:
        data = dict(as_json(decision))
        decision_id = uuid4_text(data.get("decision_id"))
        data["decision_id"] = decision_id
        with self.transaction() as connection:
            job = self.job_row(connection, job_id)
            self.check_worker(job, worker_id)
            existing = connection.execute(
                "SELECT payload FROM decisions WHERE decision_id=? AND job_id=?",
                (decision_id, str(job_id)),
            ).fetchone()
            if existing is not None:
                if existing["payload"] != json_text(data):
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "این شناسهٔ تصمیم قبلاً ثبت شده است.")
                return json.loads(existing["payload"])
            connection.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                (decision_id, str(job_id), data.get("kind", "solver_selection"),
                 data.get("policy_version", "1.0"), json_text(data), utc_now()),
            )
            if data.get("selection_status") is not None:
                self.write_job(connection, job_id, {"selection_status": data["selection_status"]})
            self.write_event(connection, job_id, "selection_decision", {
                "decision_id": decision_id, "selected_algorithm_id": data.get("selected_algorithm_id"),
            })
            return data

    def get_decision(self, job_id) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM decisions WHERE job_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (str(job_id),),
            ).fetchone()
            return None if row is None else json.loads(row["payload"])

    @staticmethod
    def execution_rows(connection, job_id) -> list[dict]:
        rows = connection.execute(
            """
            SELECT s.stage_execution_id AS execution_id, s.job_id, s.stage AS phase,
                NULL AS trial_id, NULL AS attempt_id, s.stage_execution_id,
                s.evaluation_quota, s.deadline_seconds, s.execution_status, s.known_started,
                s.reserved_active, s.charged_unknown, s.unused, s.outcome, s.history_artifact_id,
                NULL AS request, s.started_at, s.finished_at
            FROM stage_executions s WHERE s.job_id=?
            UNION ALL
            SELECT a.attempt_id, t.job_id, t.phase, t.trial_id, a.attempt_id, NULL,
                a.evaluation_quota, a.deadline_seconds, a.execution_status, a.known_started,
                a.reserved_active, a.charged_unknown, a.unused, a.outcome, a.history_artifact_id,
                t.request, a.started_at, a.finished_at
            FROM trial_attempts a JOIN trials t ON t.trial_id=a.trial_id WHERE t.job_id=?
            ORDER BY started_at, execution_id
            """,
            (str(job_id), str(job_id)),
        )
        results = [dict(row) for row in rows]
        for result in results:
            for key in ("request", "outcome"):
                if result[key] is not None:
                    result[key] = json.loads(result[key])
        return results

    def list_executions(self, job_id) -> list[dict]:
        with self.connect() as connection:
            self.job_row(connection, job_id)
            return self.execution_rows(connection, job_id)

    def list_trials(self, job_id) -> list[dict]:
        return [
            {**item["request"], "latest_attempt": {
                key: value for key, value in item.items() if key != "request"
            }}
            for item in self.list_executions(job_id) if item["trial_id"] is not None
        ]

    def start_execution(
        self, job_id, phase, quota, deadline_seconds, *, trial_request=None,
        execution_id=None, worker_id=None,
    ) -> dict:
        if phase not in {*QUOTAS, "main"} or (phase in {"pilot", "main"}) != (trial_request is not None):
            raise ValueError("Invalid numerical execution phase")
        if type(quota) is not int or quota <= 0 or not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("Invalid execution quota or deadline")
        request = as_json(trial_request)
        job_id = str(job_id)
        with self.transaction() as connection:
            job = self.job_row(connection, job_id)
            self.check_worker(job, worker_id)
            if job["job_status"] != "running":
                raise invalid_state()
            if job["cancel_requested"]:
                raise ApiError(409, "JOB_CANCEL_REQUESTED", "درخواست لغو اجرا ثبت شده است.")
            if job["problem_revision_id"] is None:
                raise ValueError("Problem must be frozen before execution")
            policy = json.loads(job["policy"])
            if phase == "main":
                fixed = sum(policy[QUOTAS[key]] for key in ("preflight", "profile", "verification"))
                pilots = policy["pilot_candidate_count"] * policy["pilot_repeats"] * policy["pilot_evaluations"]
                expected_quota = policy["max_objective_evaluations"] - fixed - pilots
            else:
                expected_quota = policy[QUOTAS[phase]]
            if quota != expected_quota:
                raise ValueError("Execution quota does not match frozen policy")

            executions = self.execution_rows(connection, job_id)
            if request is None:
                prior = next((item for item in executions if item["phase"] == phase), None)
            else:
                if (str(request["job_id"]) != job_id or request["phase"] != phase
                    or request["evaluation_quota"] != quota
                    or str(request["problem_revision_id"]) != job["problem_revision_id"]
                    or request["deadline_seconds"] != deadline_seconds):
                    raise ValueError("Trial request does not match its reservation")
                prior = next((item for item in executions if item["phase"] == phase
                    and (phase == "main" or (
                        item["request"]["algorithm_id"] == request["algorithm_id"]
                        and item["request"]["replicate_index"] == request["replicate_index"]
                    ))), None)
                if prior is not None:
                    runtime_fields = {"trial_id", "attempt_id", "idempotency_key", "deadline_seconds"}
                    previous_shape = {key: value for key, value in prior["request"].items() if key not in runtime_fields}
                    current_shape = {key: value for key, value in request.items() if key not in runtime_fields}
                    if previous_shape != current_shape:
                        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "مشخصات اجرای قبلی با درخواست یکسان نیست.")
            if prior is not None:
                return {**prior, "created": False}
            if any(item["execution_status"] == "running" for item in executions):
                raise ApiError(409, "EXECUTION_ALREADY_RUNNING", "یک فرایند عددی از قبل در حال اجرا است.")
            if phase == "pilot":
                if not 0 <= request["replicate_index"] < policy["pilot_repeats"]:
                    raise ValueError("Pilot replicate is outside frozen policy")
                algorithms = {item["request"]["algorithm_id"] for item in executions if item["phase"] == "pilot"}
                if len(algorithms | {request["algorithm_id"]}) > policy["pilot_candidate_count"]:
                    raise ValueError("Pilot candidate count exceeds frozen policy")
            if sum(item["evaluation_quota"] for item in executions) + quota > policy["max_objective_evaluations"]:
                raise ValueError("Frozen evaluation budget exceeded")

            if request is None:
                identity = uuid4_text(execution_id)
                connection.execute(
                    """INSERT INTO stage_executions (stage_execution_id,job_id,stage,evaluation_quota,
                        deadline_seconds,execution_status,reserved_active,started_at)
                    VALUES (?,?,?,?,?,'running',?,?)""",
                    (identity, job_id, phase, quota, deadline_seconds, quota, utc_now()),
                )
            else:
                identity = uuid4_text(request["attempt_id"])
                if execution_id is not None and uuid4_text(execution_id) != identity:
                    raise ValueError("Trial execution ID must be its attempt ID")
                trial_id = uuid4_text(request["trial_id"])
                logical_key = "main" if phase == "main" else (
                    f"pilot/{request['algorithm_id']}/{request['replicate_index']}"
                )
                connection.execute(
                    """INSERT INTO trials (trial_id,job_id,problem_revision_id,logical_trial_key,
                        phase,algorithm_id,config_id,seed,replicate_index,evaluation_quota,request,execution_status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,'running')""",
                    (trial_id, job_id, job["problem_revision_id"], logical_key, phase,
                     request["algorithm_id"], request["config_id"], request["seed"],
                     request["replicate_index"], quota, json_text(request)),
                )
                connection.execute(
                    """INSERT INTO trial_attempts (attempt_id,trial_id,execution_key,evaluation_quota,
                        deadline_seconds,execution_status,reserved_active,started_at)
                    VALUES (?,?,?,?,?,'running',?,?)""",
                    (identity, trial_id, request["idempotency_key"], quota, deadline_seconds, quota, utc_now()),
                )
            self.write_event(connection, job_id, "execution_started", {
                "execution_id": identity, "phase": phase, "evaluation_quota": quota,
            })
            result = next(item for item in self.execution_rows(connection, job_id) if item["execution_id"] == identity)
            return {**result, "created": True}

    @staticmethod
    def _execution_pointer(connection, execution_id):
        stage = connection.execute(
            "SELECT job_id FROM stage_executions WHERE stage_execution_id=?", (str(execution_id),)
        ).fetchone()
        if stage is not None:
            return "stage_executions", "stage_execution_id", stage["job_id"]
        trial = connection.execute(
            "SELECT t.job_id FROM trial_attempts a JOIN trials t ON t.trial_id=a.trial_id WHERE a.attempt_id=?",
            (str(execution_id),),
        ).fetchone()
        if trial is None:
            raise ApiError(404, "EXECUTION_NOT_FOUND", "اجرای عددی پیدا نشد.")
        return "trial_attempts", "attempt_id", trial["job_id"]

    def record_started(self, execution_id, count=1, *, sequence=None, worker_id=None) -> bool:
        if type(count) is not int or count <= 0:
            raise ValueError("Invalid evaluation start count")
        with self.transaction() as connection:
            table, identity, job_id = self._execution_pointer(connection, execution_id)
            job = self.job_row(connection, job_id)
            self.check_worker(job, worker_id)
            if job["cancel_requested"]:
                return False
            row = connection.execute(
                f"SELECT known_started, reserved_active, execution_status FROM {table} WHERE {identity}=?",
                (str(execution_id),),
            ).fetchone()
            if sequence is not None:
                if row["known_started"] == sequence and count == 1:
                    return True
                if row["known_started"] + count != sequence:
                    raise ValueError("Evaluation start sequence is out of order")
            if row["execution_status"] != "running" or row["reserved_active"] < count:
                raise ValueError("Execution has no active quota for this evaluation")
            connection.execute(
                f"UPDATE {table} SET known_started=known_started+?, reserved_active=reserved_active-?"
                f" WHERE {identity}=?",
                (count, count, str(execution_id)),
            )
            return True

    def finish_execution(self, execution_id, outcome, *, worker_id=None) -> dict:
        outcome = as_json(outcome)
        with self.transaction() as connection:
            _, _, job_id = self._execution_pointer(connection, execution_id)
            self.check_worker(self.job_row(connection, job_id), worker_id)
            item = next(item for item in self.execution_rows(connection, job_id)
                        if item["execution_id"] == str(execution_id))
            if item["execution_status"] == "running":
                self._finish(connection, item, outcome)
            elif json_text(item["outcome"]) != json_text(outcome):
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "نتیجهٔ اجرای عددی قبلاً ثبت شده است.")
            return next(item for item in self.execution_rows(connection, job_id)
                        if item["execution_id"] == str(execution_id))

    def _finish(self, connection, item, outcome) -> None:
        status = outcome["execution_status"]
        if status not in TERMINAL_EXECUTION:
            raise ValueError("Invalid terminal execution status")
        if outcome.get("started_evaluations", outcome.get("known_started")) != item["known_started"]:
            raise ValueError("Execution outcome does not match durable FE counter")
        artifact_id = outcome.get("history_artifact_id")
        artifact = None
        if artifact_id is not None:
            artifact = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id=? AND job_id=?",
                (str(artifact_id), item["job_id"]),
            ).fetchone()
            if artifact is None:
                raise ValueError("History artifact must be ready and belong to the execution job")
        if status == "completed":
            if not outcome.get("history_complete") or artifact is None:
                raise ValueError("Completed execution requires a complete history artifact")
            if outcome.get("history_sha256") != artifact["sha256"]:
                raise ValueError("History artifact hash differs from the execution result")
        unknown = item["reserved_active"] if status == "unknown_outcome" else 0
        unused = item["reserved_active"] - unknown
        table, identity = ("stage_executions", "stage_execution_id") if item["trial_id"] is None else (
            "trial_attempts", "attempt_id"
        )
        connection.execute(
            f"UPDATE {table} SET execution_status=?, reserved_active=0, charged_unknown=?, unused=?,"
            f" outcome=?, history_artifact_id=?, finished_at=? WHERE {identity}=?",
            (status, unknown, unused, json_text(outcome), artifact_id, utc_now(), item["execution_id"]),
        )
        if item["trial_id"] is not None:
            connection.execute("UPDATE trials SET execution_status=? WHERE trial_id=?", (status, item["trial_id"]))
        self.write_event(connection, item["job_id"], "execution_finished", {
            "execution_id": item["execution_id"], "phase": item["phase"], "execution_status": status,
            "known_started": item["known_started"], "charged_unknown": unknown, "unused": unused,
        })

    def _write_artifact(self, job_id, kind, data: bytes | str | Path, suffix) -> dict:
        job_id = uuid4_text(job_id)
        if not re.fullmatch(r"(?:\.[a-zA-Z0-9]+){0,3}", suffix):
            raise ValueError("Invalid artifact extension")
        artifact_id = uuid4_text()
        relative = Path("artifacts", job_id, f"{artifact_id}{suffix}")
        final = self.data_dir / relative
        final.parent.mkdir(parents=True, exist_ok=True)
        pending = final.with_name(final.name + ".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with pending.open("xb") as target:
                if isinstance(data, Path):
                    with data.open("rb") as source:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            target.write(block)
                            digest.update(block)
                            size += len(block)
                else:
                    block = data.encode("utf-8") if isinstance(data, str) else data
                    target.write(block)
                    digest.update(block)
                    size = len(block)
                target.flush()
                os.fsync(target.fileno())
            pending.replace(final)
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
        return {
            "artifact_id": artifact_id, "job_id": job_id, "kind": kind, "sha256": digest.hexdigest(),
            "size": size, "created_at": utc_now(), "relative_path": relative.as_posix(),
        }

    @staticmethod
    def _register_artifact(connection, artifact) -> None:
        connection.execute(
            """INSERT INTO artifacts (artifact_id,job_id,kind,relative_path,sha256,size,status,created_at)
            VALUES (:artifact_id,:job_id,:kind,:relative_path,:sha256,:size,'ready',:created_at)""",
            artifact,
        )

    def save_artifact(self, job_id, kind, data: bytes | str | Path, suffix="", *, worker_id=None) -> dict:
        artifact = self._write_artifact(job_id, kind, data, suffix)
        try:
            with self.transaction() as connection:
                self.check_worker(self.job_row(connection, job_id), worker_id)
                self._register_artifact(connection, artifact)
        except BaseException:
            (self.data_dir / artifact["relative_path"]).unlink(missing_ok=True)
            raise
        return self.public_artifact(artifact)

    @staticmethod
    def public_artifact(row) -> dict:
        return {key: row[key] for key in ("artifact_id", "job_id", "kind", "sha256", "size", "created_at")}

    def get_artifact(self, artifact_id) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM artifacts WHERE artifact_id=?", (str(artifact_id),)).fetchone()
            if row is None:
                raise ApiError(404, "ARTIFACT_NOT_FOUND", "فایل گزارش پیدا نشد.")
            return dict(row)

    def artifact_path(self, artifact_id) -> Path:
        artifact = self.get_artifact(artifact_id)
        path = (self.data_dir / artifact["relative_path"]).resolve()
        if not path.is_relative_to(self.data_dir / "artifacts" / artifact["job_id"]):
            raise ApiError(404, "ARTIFACT_NOT_FOUND", "فایل گزارش پیدا نشد.")
        if not path.is_file():
            raise ApiError(409, "ARTIFACT_NOT_READY", "فایل گزارش در دسترس نیست.")
        return path

    def publish_report(self, job_id, report, artifact_ids, *, worker_id=None, **job_status_fields) -> dict:
        job_id = str(job_id)
        report = as_json(report)
        if not job_status_fields.keys() <= JOB_PATCH:
            raise ValueError("Invalid terminal job fields")
        if job_status_fields.get("job_status") not in TERMINAL_JOB:
            raise ValueError("Report publication requires terminal job status")
        artifact_ids = [str(item) for item in artifact_ids]
        with self.transaction() as connection:
            job = self.job_row(connection, job_id)
            if job["job_status"] in TERMINAL_JOB:
                prior = connection.execute("SELECT payload FROM reports WHERE job_id=?", (job_id,)).fetchone()
                if prior is not None and prior["payload"] == json_text(report):
                    return self.job_snapshot(connection, job)
                raise invalid_state()
            self.check_worker(job, worker_id)
            if job["cancel_requested"] and job_status_fields["job_status"] != "cancelled":
                raise ApiError(409, "JOB_CANCEL_REQUESTED", "درخواست لغو اجرا ثبت شده است.")
            if connection.execute(
                "SELECT 1 FROM stage_executions WHERE job_id=? AND execution_status='running' UNION ALL "
                "SELECT 1 FROM trials WHERE job_id=? AND execution_status='running'", (job_id, job_id),
            ).fetchone() is not None:
                raise ValueError("Cannot publish report while numerical execution is active")
            for artifact_id in artifact_ids:
                if connection.execute(
                    "SELECT 1 FROM artifacts WHERE job_id=? AND artifact_id=? AND status='ready'",
                    (job_id, artifact_id),
                ).fetchone() is None:
                    raise ValueError("Report artifact must belong to job and be ready")
            connection.execute("INSERT INTO reports VALUES (?,?,?,?)",
                               (job_id, json_text(report), json_text(artifact_ids), utc_now()))
            self.write_job(connection, job_id, {
                **job_status_fields, "owner_id": None, "lease_expires_at": None,
            })
            self.write_event(connection, job_id, "job_finished", {"job_status": job_status_fields["job_status"]})
            return self.job_snapshot(connection, self.job_row(connection, job_id))

    def write_diagnostic_report(self, connection, job_id) -> None:
        """Publish bounded, known evidence when the worker cannot finish the pipeline."""
        from p_mc_optimizer.reporting import CLAIM_FA, TRIAL_COLUMNS, csv_bytes, html_bytes, json_bytes

        job = self.job_snapshot(connection, self.job_row(connection, job_id))
        rows = self.execution_rows(connection, job_id)
        revision_row = connection.execute(
            "SELECT payload FROM problem_revisions WHERE problem_revision_id=?", (job["problem_revision_id"],)
        ).fetchone()
        revision = json.loads(revision_row["payload"]) if revision_row else None
        decision_row = connection.execute(
            "SELECT payload FROM decisions WHERE job_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        decision = json.loads(decision_row["payload"]) if decision_row else None
        prior_best = [
            (row["outcome"]["best"], row) for row in rows
            if row["phase"] != "verification" and (row["outcome"] or {}).get("best") is not None
        ]
        best, origin = min(prior_best, key=lambda item: item[0]["score_for_minimization"]) if prior_best else (None, None)
        trials = []
        for row in rows:
            if row["request"] is None:
                continue
            outcome = row["outcome"] or {}
            observed = outcome.get("best") or {}
            trials.append({
                **row["request"], **outcome,
                "execution_status": row["execution_status"], "started_evaluations": row["known_started"],
                "charged_unknown": row["charged_unknown"], "objective_raw": observed.get("objective_raw"),
                "score_for_minimization": observed.get("score_for_minimization"),
                "error_code": (outcome.get("error") or {}).get("error_code"),
            })
        status_fields = {
            key: job[key] for key in ("job_status", "phase", "selection_status", "solution_status",
                                     "optimality_status", "explanation_status", "termination_reason",
                                     "partial_result", "budget", "error")
        }
        summary = (
            "اجرا لغو شد. مقدارهای قبلی فقط در اطلاعات عیب‌یابی آمده‌اند."
            if job["job_status"] == "cancelled" else
            "اجرای عددی قطع شد. سهم نامعلوم در حساب بودجه حفظ شده است."
        )
        known_elapsed = sum((row["outcome"] or {}).get("elapsed_compute_seconds", 0) for row in rows)
        has_unknown = any(row["execution_status"] == "unknown_outcome" for row in rows)
        wall_elapsed = (datetime.fromisoformat(job["updated_at"]) - datetime.fromisoformat(job["created_at"])).total_seconds()
        report = {
            "schema_version": "1.0", "job_id": job_id, **status_fields,
            "input": job["input"], "problem_revision": revision, "policy": job["policy"],
            "problem_math_hash": (revision or {}).get("problem_math_hash"),
            "evaluation_contract_hash": (revision or {}).get("evaluation_contract_hash"),
            "profile": job["workflow"].get("profile"), "candidate_plan": job["workflow"].get("candidate_plan"),
            "decision": decision, "selected_algorithm_id": (decision or {}).get("selected_algorithm_id"),
            "selected_config_id": (decision or {}).get("selected_config_id"),
            "solution_origin": None if origin is None else {
                "phase": origin["phase"], "execution_id": origin["execution_id"],
                "trial_id": origin["trial_id"], "algorithm_id": (origin["request"] or {}).get("algorithm_id"),
                "evaluation_id": best["evaluation_id"],
            },
            "best_evaluation_id": None, "best_x": None, "objective_raw": None,
            "score_for_minimization": None, "verification": None, "trials": trials, "progress": [],
            "diagnostics": {"unverified_candidate": best, "interrupted_executions": [
                {key: row[key] for key in ("execution_id", "phase", "known_started", "charged_unknown")}
                for row in rows if row["execution_status"] == "unknown_outcome"
            ]},
            "execution_history_artifacts": [row["history_artifact_id"] for row in rows if row["history_artifact_id"]],
            "llm_calls": [value for key, value in job["workflow"].items() if key.startswith("call:")],
            "cost_estimate": None, "timing": {
                "elapsed_wall_seconds": max(0.0, wall_elapsed),
                "elapsed_compute_seconds": None if has_unknown else known_elapsed,
                "known_elapsed_compute_seconds": known_elapsed,
            },
            "explanation": {"source": "template", "summary_fa": summary, "limitations_fa": [CLAIM_FA]},
        }
        exports = [
            ("report_json", json_bytes(report), ".json"),
            ("report_html", html_bytes(report), ".html"),
            ("trials_csv", csv_bytes(trials, TRIAL_COLUMNS), ".csv"),
        ]
        artifacts = []
        try:
            for kind, data, suffix in exports:
                artifact = self._write_artifact(job_id, kind, data, suffix)
                artifacts.append(artifact)
                self._register_artifact(connection, artifact)
            connection.execute(
                "INSERT INTO reports VALUES (?,?,?,?)",
                (job_id, json_text(report), json_text([item["artifact_id"] for item in artifacts]), utc_now()),
            )
        except BaseException:
            for artifact in artifacts:
                (self.data_dir / artifact["relative_path"]).unlink(missing_ok=True)
            raise

    def get_report(self, job_id) -> dict:
        with self.connect() as connection:
            connection.execute("BEGIN")
            job = self.job_row(connection, job_id)
            row = connection.execute("SELECT * FROM reports WHERE job_id=?", (str(job_id),)).fetchone()
            if row is None:
                raise ApiError(409, "ARTIFACT_NOT_READY", "گزارش این اجرا هنوز آماده نیست.", retryable=True)
            artifacts = [
                self.public_artifact(connection.execute("SELECT * FROM artifacts WHERE artifact_id=?", (item,)).fetchone())
                for item in json.loads(row["artifact_ids"])
            ]
            return {
                "report": json.loads(row["payload"]), "artifacts": artifacts,
                "complete": job["job_status"] == "completed" and not job["partial_result"],
                "partial": bool(job["partial_result"]), "explanation_status": job["explanation_status"],
            }
