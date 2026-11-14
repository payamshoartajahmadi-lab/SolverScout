"""Read-only observation of the normal local application; never prints keys/tokens."""
from datetime import datetime, UTC
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import httpx
import psutil

EVIDENCE = Path(__file__).resolve().parent
DATA = Path(r"C:\Users\TajAhmadi\AppData\Local\P_MC_Optimizer")


def client():
    info = json.loads((DATA / "runtime/instance.json").read_text(encoding="utf-8"))
    api = next(p for p in psutil.Process(info["pid"]).children() if "_api" in p.cmdline())
    return httpx.Client(
        base_url=f"http://127.0.0.1:{info['config']['api_port']}",
        headers={"X-PMC-Internal-Token": api.environ()["PMC_INTERNAL_TOKEN"]},
        trust_env=False, timeout=30,
    )


def save(name, value):
    path = EVIDENCE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def observe():
    with client() as session:
        response = session.get("/demo/preparation")
        response.raise_for_status()
        preparation = response.json()
        response = session.get("/settings/llm/profile")
        response.raise_for_status()
        profile = response.json()
        response = session.get("/runtime")
        response.raise_for_status()
        runtime = response.json()
    save("preparation/status.json", preparation)
    raw = profile.get("profile") or {}
    safe = {k: v for k, v in raw.items() if k not in {"base_url", "credential_ref"}}
    if raw.get("base_url"):
        safe["endpoint_sha256"] = sha256(raw["base_url"].encode()).hexdigest()
    credential = profile.get("credential_status") or {}
    credential = {k: v for k, v in credential.items() if k in {
        "available", "storage_mode", "credential_generation", "status", "message_code"
    }}
    manifest = json.loads((EVIDENCE / "acceptance_manifest.json").read_text(encoding="utf-8"))
    expected = {**manifest["model"], "token_limit_field": "max_completion_tokens",
                "connection_test_max_output_tokens": 512}
    differences = {k: {"expected": v, "actual": safe.get(k)}
                   for k, v in expected.items() if safe.get(k) != v} if raw else {}
    profile_status = {
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "profile_present": bool(raw), "profile": safe,
        "credential_status": credential,
        "os_keyring_available": profile.get("os_keyring_available"),
        "contract_matches": bool(raw) and not differences,
        "contract_differences": differences,
    }
    save("profile-status.json", profile_status)
    save("runtime-status.json", runtime)
    with sqlite3.connect((DATA / "state.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        calls = [dict(row) for row in db.execute(
            "SELECT call_id,purpose,role,status,started_at,finished_at FROM llm_calls ORDER BY rowid")]
    save("model-call-inventory.json", calls)
    summary = {"preparation": {k: preparation.get(k) for k in (
        "status", "prepared_count", "charged_evaluations", "reserved_evaluations", "ready", "errors")},
        "profile": profile_status, "llm_calls_count": len(calls),
        "runtime_drift": runtime.get("drift")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    observe()
