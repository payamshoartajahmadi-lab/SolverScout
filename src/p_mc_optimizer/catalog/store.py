"""SQLite storage for discovered classes, immutable snapshots and qualification."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

import psutil

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.catalog.adapter import current_library_version, discover, environment_id
from p_mc_optimizer.catalog.models import (
    ADAPTER_VERSION, CatalogSnapshot, ImplementationRecord, QualificationCheck, QualificationReport,
    SolverCardV2, SolverConfiguration, cards_hash, canonical_json, digest,
)
from p_mc_optimizer.catalog.qualification import (
    QUALIFICATION_FE_PER_CONFIG, NUMERIC_POLICY_FE_PER_CONFIG, numeric_check_matrix,
    qualify_configuration, qualify_numeric_policy,
)
from p_mc_optimizer.domain.models import DomainError

NUMERIC_POLICY_QUALIFICATION_SCHEMA = """
CREATE TABLE numeric_policy_qualification_reports (
    qualification_id TEXT NOT NULL REFERENCES qualification_runs(qualification_id),
    config_id TEXT NOT NULL REFERENCES solver_configurations(config_id),
    policy_hash TEXT NOT NULL,
    environment TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(qualification_id,config_id)
);
CREATE INDEX numeric_policy_reports_lookup_idx
    ON numeric_policy_qualification_reports(config_id,policy_hash,environment,created_at);
"""

CATALOG_SCHEMA = """
CREATE TABLE implementation_records (
    implementation_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    present INTEGER NOT NULL CHECK (present IN (0,1)),
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0,1))
);
CREATE TABLE solver_configurations (
    config_id TEXT PRIMARY KEY,
    implementation_id TEXT NOT NULL REFERENCES implementation_records(implementation_id),
    payload TEXT NOT NULL
);
CREATE TABLE qualification_runs (
    qualification_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    config_ids TEXT NOT NULL,
    max_evaluations INTEGER NOT NULL,
    max_seconds INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','completed','failed','timed_out','cancelled','interrupted')),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
    owner_pid INTEGER NOT NULL,
    owner_started REAL NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    scope TEXT NOT NULL DEFAULT 'base' CHECK(scope IN ('base','budget_horizon.v1')),
    numeric_policy_snapshot TEXT
);
CREATE TABLE qualification_checks (
    qualification_id TEXT NOT NULL REFERENCES qualification_runs(qualification_id),
    config_id TEXT NOT NULL REFERENCES solver_configurations(config_id),
    check_id TEXT NOT NULL,
    reserved_evaluations INTEGER NOT NULL,
    result TEXT,
    PRIMARY KEY(qualification_id, config_id, check_id)
);
CREATE TABLE qualification_reports (
    qualification_id TEXT NOT NULL REFERENCES qualification_runs(qualification_id),
    config_id TEXT NOT NULL REFERENCES solver_configurations(config_id),
    environment TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(qualification_id, config_id)
);
CREATE INDEX qualification_reports_config_idx ON qualification_reports(config_id,environment,created_at);
""" + NUMERIC_POLICY_QUALIFICATION_SCHEMA + """
CREATE TABLE catalog_snapshots (
    catalog_hash TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

MAX_QUALIFICATION_CONFIGS = 8
MAX_QUALIFICATION_SECONDS = 300


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CatalogStore:
    def __init__(self, db_path: Path | str, *, artifact_root: Path | str | None = None):
        self.db_path = Path(db_path)
        self.artifact_root = Path(artifact_root) if artifact_root is not None else self.db_path.parent / "catalog-artifacts"

    @contextmanager
    def _connect(self, *, write=False):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def discover(self) -> dict:
        records = discover()
        with self._connect(write=True) as connection:
            connection.execute("UPDATE implementation_records SET present=0")
            connection.executemany(
                """INSERT INTO implementation_records(implementation_id,payload,present)
                   VALUES(?,?,1) ON CONFLICT(implementation_id) DO UPDATE SET payload=excluded.payload,present=1""",
                ((record.implementation_id, record.model_dump_json()) for record in records),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO solver_configurations(config_id,implementation_id,payload) VALUES(?,?,?)",
                ((record.configuration.config_id, record.implementation_id, record.configuration.model_dump_json())
                 for record in records if record.configuration is not None),
            )
        result = self.list_implementations(limit=1)
        result.pop("items")
        result.update(discovered_count=len(records), library_version=records[0].library_version if records else None)
        return result

    def _inventory(self, connection):
        rows = connection.execute(
            "SELECT payload,disabled FROM implementation_records WHERE present=1 ORDER BY implementation_id"
        ).fetchall()
        reports = {}
        for row in connection.execute(
            """SELECT config_id,payload FROM qualification_reports
               WHERE environment=? ORDER BY created_at,qualification_id""", (environment_id(),),
        ):
            report = QualificationReport.model_validate_json(row["payload"])
            if report.status != "cancelled":
                reports[row["config_id"]] = report
        return [(ImplementationRecord.model_validate_json(row["payload"]), row["disabled"]) for row in rows], reports

    def _latest_cards(self, inventory, reports):
        cards = []
        library_version = current_library_version()
        for row, disabled in inventory:
            config = row.configuration
            report = reports.get(config.config_id) if config is not None else None
            if (
                disabled or report is None or report.status != "qualified"
                or config.library_version != library_version or config.adapter_version != ADAPTER_VERSION
            ):
                continue
            cards.append(SolverCardV2(
                implementation_id=row.implementation_id, config_id=config.config_id, family_id=row.family_id,
                library_version=config.library_version, adapter_version=config.adapter_version,
                seed_identity=row.seed_identity, configuration=config, qualified_report=report,
                pilot_500_compatible=report.pilot_500_compatible,
            ))
        return tuple(cards)

    @staticmethod
    def _persist_snapshot(connection, cards: tuple[SolverCardV2, ...]) -> CatalogSnapshot:
        snapshot = CatalogSnapshot(catalog_hash=cards_hash(cards), cards=cards)
        connection.execute(
            "INSERT OR IGNORE INTO catalog_snapshots(catalog_hash,payload,created_at) VALUES(?,?,?)",
            (snapshot.catalog_hash, snapshot.model_dump_json(), _now()),
        )
        return snapshot

    def snapshot(self) -> CatalogSnapshot:
        with self._connect(write=True) as connection:
            return self._persist_snapshot(connection, self._latest_cards(*self._inventory(connection)))

    def get_snapshot(self, catalog_hash: str) -> CatalogSnapshot:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM catalog_snapshots WHERE catalog_hash=?", (catalog_hash,),
            ).fetchone()
        if row is None:
            raise DomainError("CATALOG_SNAPSHOT_NOT_FOUND", "نسخهٔ کاتالوگ انتخاب‌شده پیدا نشد.")
        return CatalogSnapshot.model_validate_json(row["payload"])

    def numeric_execution_snapshot(self, catalog_snapshot: CatalogSnapshot) -> dict:
        from p_mc_optimizer.catalog.budget_policy import (
            ALLOWLIST, POLICY_DEFINITION, POLICY_HASH, numeric_runtime_fingerprint, numeric_runtime_fingerprint_hash,
        )

        fingerprint = numeric_runtime_fingerprint()
        fingerprint_hash = numeric_runtime_fingerprint_hash()
        certificates, exclusions, base_reports = {}, [], {}
        with self._connect() as connection:
            for card in catalog_snapshot.cards:
                report = card.qualified_report.model_dump(mode="json")
                base_reports[card.config_id] = {"report_hash": digest(report), "report": report}
                if card.implementation_id not in ALLOWLIST:
                    continue
                rows = connection.execute(
                    """SELECT p.payload,p.report_hash,p.qualification_id FROM numeric_policy_qualification_reports p
                       JOIN qualification_runs r USING(qualification_id)
                       WHERE p.config_id=? AND p.policy_hash=? AND p.environment=?
                         AND r.status IN ('completed','timed_out') AND r.cancel_requested=0
                       ORDER BY p.created_at DESC,p.qualification_id DESC""",
                    (card.config_id, POLICY_HASH, fingerprint_hash),
                ).fetchall()
                latest = next((row for row in rows if json.loads(row["payload"])["status"] != "cancelled"), None)
                payload = json.loads(latest["payload"]) if latest else None
                if fingerprint["mealpy_version"] != POLICY_DEFINITION["library_version"]:
                    code = "NUMERIC_POLICY_ENVIRONMENT_MISMATCH"
                elif (payload and payload["status"] == "qualified" and latest["report_hash"] == digest(payload)
                      and payload.get("schema_version") == "1.0"
                      and payload.get("qualification_id") == latest["qualification_id"]
                      and payload.get("base_config_id") == card.config_id
                      and payload.get("implementation_id") == card.implementation_id
                      and payload.get("policy_id") == "budget_horizon.v1"
                      and payload.get("policy_hash") == POLICY_HASH
                      and payload.get("numeric_runtime_fingerprint_sha256") == fingerprint_hash
                      and payload.get("numeric_runtime_fingerprint") == fingerprint
                      and payload.get("pilot_500_compatible") is True):
                    certificates[card.config_id] = {"certificate_hash": latest["report_hash"], "certificate": payload}
                    continue
                else:
                    code = "NUMERIC_POLICY_QUALIFICATION_REQUIRED"
                exclusions.append({"base_config_id": card.config_id, "implementation_id": card.implementation_id,
                                   "error_code": code})
        return {"schema_version": "1.0", "policy_id": "budget_horizon.v1", "policy_definition": deepcopy(POLICY_DEFINITION),
                "policy_hash": POLICY_HASH, "numeric_runtime_fingerprint": fingerprint,
                "numeric_runtime_fingerprint_sha256": fingerprint_hash, "certificates": certificates,
                "base_qualification_reports": base_reports, "exclusions": exclusions}

    def _numeric_batch_snapshot(self, configs, base_reports):
        from p_mc_optimizer.catalog.budget_policy import (
            ALLOWLIST, POLICY_DEFINITION, POLICY_HASH, numeric_runtime_fingerprint, numeric_runtime_fingerprint_hash,
        )

        if len(configs) > 2 or any(config.implementation_id not in ALLOWLIST for config in configs):
            raise ApiError(422, "CATALOG_QUALIFICATION_INVALID", "این آزمون فقط دو پیکربندی GWO و WOA را می‌پذیرد.")
        if any(config.config_id not in base_reports or base_reports[config.config_id].status != "qualified" for config in configs):
            raise ApiError(422, "NUMERIC_POLICY_QUALIFICATION_REQUIRED", "ابتدا سازگاری پایهٔ پیکربندی را تأیید کنید.")
        fingerprint = numeric_runtime_fingerprint()
        if fingerprint["mealpy_version"] != POLICY_DEFINITION["library_version"]:
            raise ApiError(409, "NUMERIC_POLICY_ENVIRONMENT_MISMATCH", "نسخهٔ کتابخانه با سیاست عددی یکسان نیست.")
        return {"schema_version": "1.0", "policy_id": "budget_horizon.v1", "policy_definition": deepcopy(POLICY_DEFINITION),
                "policy_hash": POLICY_HASH, "numeric_runtime_fingerprint": fingerprint,
                "numeric_runtime_fingerprint_sha256": numeric_runtime_fingerprint_hash(),
                "base_configurations": {config.config_id: config.model_dump(mode="json") for config in configs},
                "base_qualification_reports": {config.config_id: {
                    "report_hash": digest(base_reports[config.config_id].model_dump(mode="json")),
                    "report": base_reports[config.config_id].model_dump(mode="json"),
                } for config in configs},
                "qualification_profile_id": "budget_horizon_checks.v1", "check_matrix": numeric_check_matrix()}

    def list_implementations(
        self, *, limit: int = 100, offset: int = 0, family: str | None = None,
        status: str | None = None, query: str = "", pilot_500_compatible: bool | None = None,
    ) -> dict:
        with self._connect(write=True) as connection:
            inventory, reports = self._inventory(connection)
            snapshot = self._persist_snapshot(connection, self._latest_cards(inventory, reports))
        numeric = self.numeric_execution_snapshot(snapshot)
        numeric_exclusions = {row["base_config_id"]: row["error_code"] for row in numeric["exclusions"]}
        items, suggested, counts = [], [], {
            "discovered": 0, "needs_configuration": 0, "qualified": 0, "rejected": 0, "disabled": 0,
        }
        library_version = current_library_version()
        for row, disabled in inventory:
            config_id = row.configuration.config_id if row.configuration else None
            report = reports.get(config_id)
            version_mismatch = row.library_version != library_version or (
                row.configuration is not None and row.configuration.adapter_version != ADAPTER_VERSION
            )
            if version_mismatch:
                state = "needs_configuration"
            elif disabled:
                state = "disabled"
            elif report:
                state = report.status
            elif not config_id:
                state = "needs_configuration"
            else:
                state = "discovered"
            counts[state] += 1
            data = row.model_dump(mode="json") | {
                "status": state, "config_id": config_id,
                "qualification": report.model_dump(mode="json") if report else None,
                "budget_policy_qualification": numeric["certificates"].get(config_id),
                "budget_policy_status": "qualified" if config_id in numeric["certificates"] else (
                    numeric_exclusions.get(config_id, "not_applicable")
                ),
                "reason": "SOLVER_CONFIGURATION_MISMATCH" if version_mismatch else (
                    row.reason if report is None or report.status == "qualified" else "QUALIFICATION_FAILED"
                ),
            }
            if row.suggested and config_id is not None and not disabled and not version_mismatch:
                suggested.append({key: data[key] for key in (
                    "implementation_id", "class_name", "config_id", "status", "budget_policy_status",
                )})
            if (
                (family is None or row.family_id == family)
                and (status is None or status == state)
                and query.casefold() in row.implementation_id.casefold()
                and (pilot_500_compatible is None or pilot_500_compatible == (
                    state == "qualified" and report.pilot_500_compatible
                ))
            ):
                items.append(data)
        return {
            "schema_version": "2.0", "items": items[offset:offset + limit], "total": len(items),
            "counts": counts, "discovered_count": len(inventory), "qualified_count": len(snapshot.cards),
            "family_ids": sorted({row.family_id for row, _ in inventory}), "catalog_hash": snapshot.catalog_hash,
            "suggested_configurations": suggested,
            "qualification_fe_per_config": QUALIFICATION_FE_PER_CONFIG,
            "budget_policy_fe_per_config": NUMERIC_POLICY_FE_PER_CONFIG,
        }

    def set_availability(
        self, implementation_id: str, *, config_id: str, expected_disabled: bool, disabled: bool,
    ) -> dict:
        with self._connect(write=True) as connection:
            row = connection.execute(
                "SELECT payload,disabled FROM implementation_records WHERE implementation_id=? AND present=1",
                (implementation_id,),
            ).fetchone()
            if row is None:
                raise ApiError(404, "IMPLEMENTATION_NOT_FOUND", "روش در کاتالوگ فعلی پیدا نشد.")
            record = ImplementationRecord.model_validate_json(row["payload"])
            if record.configuration is None or record.configuration.config_id != config_id:
                raise ApiError(409, "CATALOG_CONFIGURATION_CHANGED", "پیکربندی روش تغییر کرده است. کاتالوگ را بازخوانی کنید.")
            if bool(row["disabled"]) != disabled:
                if bool(row["disabled"]) != expected_disabled:
                    raise ApiError(409, "CATALOG_CONFIGURATION_CHANGED", "وضعیت روش تغییر کرده است. کاتالوگ را بازخوانی کنید.")
                connection.execute(
                    "UPDATE implementation_records SET disabled=? WHERE implementation_id=?",
                    (int(disabled), implementation_id),
                )
            snapshot = self._persist_snapshot(connection, self._latest_cards(*self._inventory(connection)))
        return {"implementation_id": implementation_id, "disabled": disabled, "catalog_hash": snapshot.catalog_hash}

    @staticmethod
    def _reconcile(connection):
        for row in connection.execute("SELECT * FROM qualification_runs WHERE status='running'"):
            try:
                alive = psutil.Process(row["owner_pid"]).create_time() == row["owner_started"]
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                alive = False
            except psutil.AccessDenied:
                alive = True
            if not alive:
                connection.execute(
                    "UPDATE qualification_runs SET status='interrupted',finished_at=? WHERE qualification_id=?",
                    (_now(), row["qualification_id"]),
                )

    def reserve_batch(self, config_ids: list[str], *, max_seconds: int, idempotency_key: str,
                      scope: str = "base") -> tuple[str, bool]:
        if (
            not 1 <= len(config_ids) <= MAX_QUALIFICATION_CONFIGS
            or len(config_ids) != len(set(config_ids))
            or not 1 <= max_seconds <= MAX_QUALIFICATION_SECONDS
            or not 1 <= len(idempotency_key) <= 200
            or scope not in {"base", "budget_horizon.v1"}
        ):
            raise ApiError(422, "CATALOG_QUALIFICATION_INVALID", "تعداد روش‌ها یا سقف زمان آزمون معتبر نیست.")
        config_ids = sorted(config_ids)
        with self._connect(write=True) as connection:
            self._reconcile(connection)
            previous = connection.execute(
                "SELECT * FROM qualification_runs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous is not None:
                frozen = json.loads(previous["numeric_policy_snapshot"] or "null")
                request_hash = self._qualification_request_hash(config_ids, max_seconds, scope, frozen)
                if (previous["scope"] != scope or json.loads(previous["config_ids"]) != config_ids
                        or previous["max_seconds"] != max_seconds or previous["request_hash"] != request_hash):
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "مشخصات آزمون با درخواست قبلی مطابقت ندارد.")
                return previous["qualification_id"], False
            if connection.execute("SELECT 1 FROM qualification_runs WHERE status='running'").fetchone():
                raise ApiError(409, "CATALOG_QUALIFICATION_RUNNING", "یک آزمون سازگاری در حال اجراست.")
            inventory, base_reports = self._inventory(connection)
            library_version = current_library_version()
            available = {
                row.configuration.config_id for row, disabled in inventory
                if row.configuration and not disabled and row.library_version == library_version
                and row.configuration.adapter_version == ADAPTER_VERSION
            }
            if not set(config_ids) <= available:
                raise ApiError(422, "CATALOG_QUALIFICATION_INVALID", "پیکربندی برای آزمون در کاتالوگ فعلی موجود نیست.")
            configs = [row.configuration for row, _ in inventory if row.configuration and row.configuration.config_id in config_ids]
            snapshot = self._numeric_batch_snapshot(configs, base_reports) if scope != "base" else None
            request_hash = self._qualification_request_hash(config_ids, max_seconds, scope, snapshot)
            qualification_id = str(uuid4())
            connection.execute(
                """INSERT INTO qualification_runs(qualification_id,idempotency_key,request_hash,config_ids,max_evaluations,
                   max_seconds,status,owner_pid,owner_started,created_at,scope,numeric_policy_snapshot)
                   VALUES(?,?,?,?,?,?,'running',?,?,?,?,?)""",
                (qualification_id, idempotency_key, request_hash, canonical_json(config_ids),
                 len(config_ids) * (QUALIFICATION_FE_PER_CONFIG if scope == "base" else NUMERIC_POLICY_FE_PER_CONFIG),
                 max_seconds, os.getpid(), psutil.Process().create_time(), _now(), scope,
                 canonical_json(snapshot) if snapshot else None),
            )
            return qualification_id, True

    @staticmethod
    def _qualification_request_hash(config_ids, max_seconds, scope, snapshot):
        if scope == "base":
            return digest([config_ids, max_seconds])
        if snapshot is None:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "snapshot آزمون با درخواست قبلی مطابقت ندارد.")
        return digest({"scope": scope, "config_ids": config_ids, "max_seconds": max_seconds,
                       "policy_hash": snapshot["policy_hash"],
                       "numeric_runtime_fingerprint_sha256": snapshot["numeric_runtime_fingerprint_sha256"]})

    def _batch_row(self, connection, qualification_id):
        row = connection.execute(
            "SELECT * FROM qualification_runs WHERE qualification_id=?", (qualification_id,),
        ).fetchone()
        if row is None:
            raise ApiError(404, "CATALOG_QUALIFICATION_NOT_FOUND", "آزمون سازگاری پیدا نشد.")
        return row

    def batch(self, qualification_id: str) -> dict:
        with self._connect(write=True) as connection:
            self._reconcile(connection)
            row = self._batch_row(connection, qualification_id)
            report_table = "qualification_reports" if row["scope"] == "base" else "numeric_policy_qualification_reports"
            checks, reports = connection.execute(
                "SELECT * FROM qualification_checks WHERE qualification_id=? ORDER BY rowid", (qualification_id,),
            ).fetchall(), connection.execute(
                f"SELECT * FROM {report_table} WHERE qualification_id=? ORDER BY config_id", (qualification_id,),
            ).fetchall()
        results = [json.loads(check["result"]) for check in checks if check["result"]]
        return {
            "schema_version": "2.0",
            "scope": row["scope"],
            **{key: row[key] for key in ("qualification_id", "status", "max_evaluations", "max_seconds", "created_at", "finished_at")},
            "config_ids": json.loads(row["config_ids"]), "cancel_requested": bool(row["cancel_requested"]),
            "checks_completed": len(results), "started_evaluations": sum(result["started_evaluations"] for result in results),
            "charged_unknown": sum(result["charged_unknown"] for result in results) + (
                sum(check["reserved_evaluations"] for check in checks if not check["result"])
                if row["status"] in {"interrupted", "failed", "timed_out", "cancelled"} else 0
            ),
            "reports": [json.loads(report["payload"]) for report in reports],
            "report_hashes": {report["config_id"]: report["report_hash"] for report in reports}
                if row["scope"] != "base" else {},
            "reserved_evaluations": sum(check["reserved_evaluations"] for check in checks),
            "reserved_active": sum(check["reserved_evaluations"] for check in checks if not check["result"])
                if row["status"] == "running" else 0,
            "unused": sum(check["reserved_evaluations"] - json.loads(check["result"])["started_evaluations"]
                          - json.loads(check["result"])["charged_unknown"] for check in checks if check["result"]),
        }

    def cancel_batch(self, qualification_id: str) -> dict:
        with self._connect(write=True) as connection:
            self._batch_row(connection, qualification_id)
            connection.execute(
                "UPDATE qualification_runs SET cancel_requested=1 WHERE qualification_id=? AND status='running'",
                (qualification_id,),
            )
        return self.batch(qualification_id)

    def run_batch(self, qualification_id: str) -> None:
        with self._connect() as connection:
            if self._batch_row(connection, qualification_id)["status"] != "running":
                return
        status = "failed"

        def cancelled():
            with self._connect() as connection:
                return bool(self._batch_row(connection, qualification_id)["cancel_requested"])

        try:
            with self._connect() as connection:
                row = self._batch_row(connection, qualification_id)
                snapshot = json.loads(row["numeric_policy_snapshot"] or "null")
                if snapshot is not None:
                    configs = [SolverConfiguration.model_validate_json(canonical_json(value))
                               for _, value in sorted(snapshot["base_configurations"].items())]
                else:
                    configs = [SolverConfiguration.model_validate_json(connection.execute(
                        "SELECT payload FROM solver_configurations WHERE config_id=?", (config_id,),
                    ).fetchone()["payload"]) for config_id in json.loads(row["config_ids"])]
            deadline = monotonic() + row["max_seconds"]
            for config in configs:
                if cancelled() or monotonic() >= deadline:
                    break

                def reserve(check_id: str, quota: int):
                    with self._connect(write=True) as connection:
                        connection.execute(
                            """INSERT INTO qualification_checks(qualification_id,config_id,check_id,reserved_evaluations)
                               VALUES(?,?,?,?)""", (qualification_id, config.config_id, check_id, quota),
                        )

                def finish(check: QualificationCheck):
                    with self._connect(write=True) as connection:
                        connection.execute(
                            "UPDATE qualification_checks SET result=? WHERE qualification_id=? AND config_id=? AND check_id=?",
                            (check.model_dump_json(), qualification_id, config.config_id, check.check),
                        )

                if snapshot is None:
                    report = qualify_configuration(
                        config, history_directory=self.artifact_root / qualification_id / config.config_id,
                        deadline=deadline, cancelled=cancelled, reserve=reserve, finish=finish,
                    )
                else:
                    report = qualify_numeric_policy(
                        config, snapshot=snapshot, qualification_id=qualification_id,
                        history_directory=self.artifact_root / qualification_id / config.config_id,
                        deadline=deadline, cancelled=cancelled, finish=finish,
                        reserve=lambda entry, request: self._reserve_numeric_check(qualification_id, config, snapshot, entry, request),
                    )
                with self._connect(write=True) as connection:
                    if snapshot is None:
                        connection.execute(
                            """INSERT INTO qualification_reports(qualification_id,config_id,environment,payload,created_at)
                               VALUES(?,?,?,?,?)""",
                            (qualification_id, config.config_id, report.environment, report.model_dump_json(), _now()),
                        )
                    else:
                        connection.execute(
                            """INSERT INTO numeric_policy_qualification_reports
                               (qualification_id,config_id,policy_hash,environment,report_hash,payload,created_at)
                               VALUES(?,?,?,?,?,?,?)""",
                            (qualification_id, config.config_id, snapshot["policy_hash"], snapshot["numeric_runtime_fingerprint_sha256"],
                             digest(report), canonical_json(report), report["created_at_utc"]),
                        )
            status = "cancelled" if cancelled() else ("timed_out" if monotonic() >= deadline else "completed")
        finally:
            with self._connect(write=True) as connection:
                connection.execute(
                    "UPDATE qualification_runs SET status=?,finished_at=? WHERE qualification_id=?",
                    (status, _now(), qualification_id),
                )

    def _reserve_numeric_check(self, qualification_id, config, snapshot, entry, request):
        from p_mc_optimizer.catalog.budget_policy import (
            POLICY_DEFINITION, POLICY_HASH, _NumericQualificationAuthority, numeric_runtime_fingerprint_hash,
        )
        from p_mc_optimizer.problems.registry import freeze_problem
        from p_mc_optimizer.domain.models import ProblemInput
        from p_mc_optimizer.selection.policies import derive_seed
        from p_mc_optimizer.catalog.adapter import seed_identity

        with self._connect(write=True) as connection:
            run = self._batch_row(connection, qualification_id)
            frozen = json.loads(run["numeric_policy_snapshot"] or "null")
            if (snapshot["qualification_profile_id"] != "budget_horizon_checks.v1"
                    or snapshot["check_matrix"] != numeric_check_matrix()
                    or snapshot["policy_definition"] != POLICY_DEFINITION or snapshot["policy_hash"] != POLICY_HASH):
                raise DomainError("NUMERIC_POLICY_ENVIRONMENT_MISMATCH", "تعریف اجرایی آزمون با snapshot یکسان نیست.")
            base = snapshot["base_qualification_reports"].get(config.config_id)
            if (run["status"] != "running" or run["scope"] != "budget_horizon.v1" or frozen != snapshot
                    or entry not in snapshot["check_matrix"] or base is None
                    or base["report_hash"] != digest(base["report"]) or base["report"]["status"] != "qualified"
                    or config.model_dump(mode="json") != snapshot["base_configurations"].get(config.config_id)):
                raise DomainError("NUMERIC_POLICY_AUTHORIZATION_INVALID", "مجوز آزمون با رزرو پایدار مطابقت ندارد.")
            if numeric_runtime_fingerprint_hash() != snapshot["numeric_runtime_fingerprint_sha256"]:
                raise DomainError("NUMERIC_POLICY_ENVIRONMENT_MISMATCH", "محیط آزمون با snapshot یکسان نیست.")
            revision = freeze_problem(ProblemInput.model_validate(entry["problem_specification"]))
            seed = derive_seed(1405, revision.problem_math_hash, "budget_policy_qualification", seed_identity(config.implementation_id), 0)
            if (request.revision.problem_math_hash != revision.problem_math_hash or request.seed != seed
                    or request.phase != entry["phase"] or request.evaluation_quota != entry["evaluation_quota"]
                    or request.trial.replicate_index != entry["replicate_index"]):
                raise DomainError("NUMERIC_POLICY_AUTHORIZATION_INVALID", "درخواست با ماتریس آزمون یکسان نیست.")
            connection.execute(
                """INSERT INTO qualification_checks(qualification_id,config_id,check_id,reserved_evaluations)
                   VALUES(?,?,?,?)""", (qualification_id, config.config_id, entry["check_id"], request.evaluation_quota),
            )
            return _NumericQualificationAuthority(
                qualification_id=qualification_id, check_id=entry["check_id"], execution_id=request.execution_id,
                base_config_id=config.config_id, policy_hash=snapshot["policy_hash"],
                numeric_runtime_fingerprint_sha256=snapshot["numeric_runtime_fingerprint_sha256"],
                evaluation_quota=request.evaluation_quota, phase=request.phase,
                request_hash=digest(request.model_dump(mode="json")),
            )
