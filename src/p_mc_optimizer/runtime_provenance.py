"""Immutable observations of each real process and read-only disk drift checks."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import subprocess
from threading import RLock
from uuid import UUID, uuid4

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


PACKAGE_ROOT = Path(__file__).resolve().parent
_LOCK = RLock()
_PAYLOAD = None
_PID = None
_ROLES = {"api", "worker", "numeric", "headless", "supervisor", "ui"}


class RuntimeProvenanceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["runtime-provenance/1.0"]
    process_run_id: str
    role: Literal["api", "worker", "numeric", "headless", "supervisor", "ui"]
    instance_id: str | None
    pid: int = Field(gt=0)
    started_at_utc: str
    project_version: str
    git_commit_observed_at_startup: str | None
    git_dirty_observed_at_startup: bool | None
    source_observation: Literal["startup_manifest"]
    source_manifest_sha256: str | None
    source_manifest_status: Literal["complete", "unavailable"]
    python_version: str
    dependency_versions: dict[str, str | None]

    @field_validator("process_run_id")
    @classmethod
    def valid_run_id(cls, value):
        if UUID(value).version != 4:
            raise ValueError("Runtime run IDs must be UUID4")
        return value

    @field_validator("started_at_utc")
    @classmethod
    def utc_start_time(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("Runtime timestamps must use UTC")
        return value


def canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def source_manifest(root: Path | None = None) -> list[dict]:
    root = PACKAGE_ROOT if root is None else Path(root)
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix in {".py", ".txt"}
                   and "__pycache__" not in path.parts)
    if not paths:
        raise OSError("No package source files are available")
    return [{"path": path.relative_to(root).as_posix(), "sha256": sha256(path.read_bytes()).hexdigest()}
            for path in paths]


def _git_observation():
    try:
        options = {"cwd": PACKAGE_ROOT, "text": True, "stderr": subprocess.DEVNULL, "timeout": 5}
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], **options).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain=v1"], **options))
        return commit, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def startup(role="headless", *, instance_id=None, data_dir=None) -> dict:
    global _PAYLOAD, _PID
    if role not in _ROLES:
        raise ValueError("Unknown runtime role")
    with _LOCK:
        if _PAYLOAD is None or _PID != os.getpid():
            try:
                manifest = source_manifest()
                digest, status = sha256(canonical_bytes(manifest)).hexdigest(), "complete"
            except OSError:
                manifest, digest, status = [], None, "unavailable"
            dependencies = {}
            for name in ("numpy", "mealpy", "pydantic", "httpx", "langgraph", "fastapi", "streamlit"):
                try:
                    dependencies[name] = version(name)
                except PackageNotFoundError:
                    dependencies[name] = None
            from p_mc_optimizer import __version__

            commit, dirty = _git_observation()
            identity = {
                "schema_version": "runtime-provenance/1.0", "process_run_id": str(uuid4()), "role": role,
                "instance_id": instance_id if instance_id is not None else os.environ.get("PMC_INSTANCE_ID"),
                "pid": os.getpid(), "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "project_version": __version__, "git_commit_observed_at_startup": commit,
                "git_dirty_observed_at_startup": dirty, "source_observation": "startup_manifest",
                "source_manifest_sha256": digest, "source_manifest_status": status,
                "python_version": platform.python_version(), "dependency_versions": dependencies,
            }
            _PAYLOAD, _PID = {"identity": identity, "source_manifest": manifest}, os.getpid()
        result = deepcopy(_PAYLOAD)
    from p_mc_optimizer.catalog.budget_policy import numeric_runtime_fingerprint

    numeric_runtime_fingerprint()
    if data_dir is not None:
        save_runtime(data_dir, result)
    return result["identity"]


def get_runtime(role=None) -> dict:
    return startup(role or "headless")


def runtime_payload(role=None) -> dict:
    startup(role or "headless")
    with _LOCK:
        return deepcopy(_PAYLOAD)


def validate_runtime_payload(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"identity", "source_manifest"}:
        raise ValueError("Invalid runtime provenance envelope")
    identity, manifest = payload["identity"], payload["source_manifest"]
    identity = RuntimeProvenanceV1.model_validate(identity).model_dump(mode="json")
    if UUID(identity["process_run_id"]).version != 4 or identity.get("role") not in _ROLES:
        raise ValueError("Invalid runtime process identity")
    if type(identity.get("pid")) is not int or identity["pid"] <= 0:
        raise ValueError("Invalid runtime PID")
    if not isinstance(manifest, list):
        raise ValueError("Invalid runtime source manifest")
    for row in manifest:
        path = Path(row["path"])
        if set(row) != {"path", "sha256"} or path.is_absolute() or ".." in path.parts:
            raise ValueError("Invalid runtime manifest entry")
        if len(row["sha256"]) != 64 or any(char not in "0123456789abcdef" for char in row["sha256"]):
            raise ValueError("Invalid runtime manifest digest")
    if identity["source_manifest_status"] == "complete":
        if sha256(canonical_bytes(manifest)).hexdigest() != identity["source_manifest_sha256"]:
            raise ValueError("Runtime manifest hash mismatch")
    elif identity["source_manifest_status"] != "unavailable" or identity["source_manifest_sha256"] is not None:
        raise ValueError("Invalid runtime source status")
    return identity


def save_runtime(data_dir, payload=None) -> None:
    payload = runtime_payload() if payload is None else payload
    identity = validate_runtime_payload(payload)
    directory = Path(data_dir) / "runtime" / "provenance"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{identity['process_run_id']}.json"
    body = canonical_bytes(payload)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != body:
            raise ValueError("A persisted runtime identity cannot be changed")
    else:
        with os.fdopen(descriptor, "wb") as output:
            output.write(body)


def runtime_status(data_dir=None) -> dict:
    identity = get_runtime()
    drift = identity_drift(identity)

    processes = []
    if data_dir is not None:
        for path in sorted((Path(data_dir) / "runtime" / "provenance").glob("*.json")):
            try:
                saved = validate_runtime_payload(json.loads(path.read_bytes()))
                if saved["instance_id"] == identity["instance_id"]:
                    processes.append(saved)
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return {"identity": identity, "drift": drift, "processes": processes,
            "message_code": "RUNTIME_RESTART_REQUIRED" if drift == "changed" else None}


def identity_drift(identity) -> str:
    try:
        observed = sha256(canonical_bytes(source_manifest())).hexdigest()
        drift = "unknown" if identity["source_manifest_sha256"] is None else (
            "unchanged" if observed == identity["source_manifest_sha256"] else "changed"
        )
    except OSError:
        drift = "unknown"
    return drift


def inspect_instance(data_dir, instance_id) -> dict:
    identities = []
    for path in (Path(data_dir) / "runtime" / "provenance").glob("*.json"):
        try:
            identity = validate_runtime_payload(json.loads(path.read_bytes()))
            if identity["instance_id"] == instance_id:
                identities.append(identity)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    api = next((identity for identity in identities if identity["role"] == "api"), None)
    drift = identity_drift(api) if api is not None else "unknown"
    return {"identity": api, "processes": identities, "drift": drift,
            "message_code": "RUNTIME_RESTART_REQUIRED" if drift == "changed" else None}


def report_runtime_provenance(repo, job_id) -> dict:
    workflow = repo.get_job(job_id)["workflow"]
    processes = {}
    creator = workflow.get("runtime:creator")
    workers = workflow.get("runtime:workers", {})
    identities = [creator, *[row.get("identity", row) for row in workers.values()],
                  *workflow.get("runtime:numeric", {}).values()]
    for row in repo.list_executions(job_id):
        outcome = row.get("outcome") or {}
        child = outcome.get("runtime_provenance")
        if child:
            identities.append(child.get("identity", child))
    with repo.connect() as connection:
        for row in connection.execute("SELECT metadata FROM llm_calls WHERE job_id=?", (job_id,)):
            provenance = (json.loads(row[0] or "null") or {}).get("provenance", {})
            if provenance.get("gateway"):
                identities.append(provenance["gateway"])
    for identity in identities:
        if isinstance(identity, dict) and identity.get("process_run_id"):
            processes[identity["process_run_id"]] = identity
    status = "legacy_unknown" if not processes else (
        "complete" if creator and workers and all((row.get("outcome") or {}).get("runtime_provenance")
                                                for row in repo.list_executions(job_id)) else "partial"
    )
    return {"status": status, "processes": list(processes.values())}
