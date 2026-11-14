"""Capture an explicitly selected offline pytest run without importing app code."""
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collect_evidence import runtime_manifest, harness_manifest

evidence = Path(__file__).resolve().parent
workspace = evidence.parents[2]
label, *selection = sys.argv[1:]
if not label or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in label):
    raise SystemExit("A safe run label is required")
result_path = evidence / "offline" / (label + "-result.json")
if result_path.exists():
    raise SystemExit("Refusing to overwrite an earlier run")
log_path = evidence / "offline" / (label + ".log")
junit_path = evidence / "offline" / (label + ".xml")
guard_path = evidence / "offline" / (label + "-network-guard.json")
env = os.environ.copy()
for name in ("PMC_TEST_LIVE_PROVIDER_CONFIG", "PYTEST_ADDOPTS", "OPENAI_API_KEY",
             "AZURE_OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    env.pop(name, None)
env.update(PYTHONPATH=os.pathsep.join((str(workspace / "src"), str(evidence))),
           PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
           SOLVERSCOUT_OFFLINE_GUARD_REPORT=str(guard_path))
command = [sys.executable, "-m", "pytest", "-q", "-m", "not live_provider",
           "-p", "offline_network_guard", "--junitxml=" + str(junit_path), *selection]
started = datetime.now(timezone.utc).isoformat()
source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
source_changes = subprocess.check_output(
    ["git", "status", "--porcelain", "--", ".gitattributes", "src", "tests", "scripts", "schemas",
     "pyproject.toml", "run_server.py", "requirements.lock", "requirements-test.lock", "examples/config.example.json"],
    cwd=workspace, text=True).splitlines()
frozen_manifests = {"source": runtime_manifest(), "harness": harness_manifest()}
for kind, manifest in frozen_manifests.items():
    (evidence / "offline" / (label + "-" + kind + "_manifest.json")).write_text(
        json.dumps({**manifest, "tested_commit": source_sha, "capture_stage": "before pytest"}, indent=2) + "\n",
        encoding="utf-8")
start = time.monotonic()
print("RUN", subprocess.list2cmdline(command), flush=True)
with log_path.open("w", encoding="utf-8", newline="") as log:
    process = subprocess.Popen(command, cwd=workspace, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                               errors="replace", bufsize=1)
    for line in process.stdout:
        log.write(line)
        log.flush()
        print(line, end="", flush=True)
    code = process.wait()
totals = None
if junit_path.exists():
    root = ET.parse(junit_path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    totals = {name: sum(int(suite.get(name, "0")) for suite in suites)
              for name in ("tests", "failures", "errors", "skipped")}
    totals["passed"] = totals["tests"] - sum(totals[k] for k in ("failures", "errors", "skipped"))
result = {
    "run_label": label, "started_at_utc": started,
    "finished_at_utc": datetime.now(timezone.utc).isoformat(),
    "elapsed_seconds": round(time.monotonic() - start, 3),
    "cwd": str(workspace), "command_argv": command, "exit_code": code,
    "head_sha": source_sha, "source_changes_at_start": source_changes,
    "source_sha256": frozen_manifests["source"]["canonical_source_sha256"],
    "harness_sha256": frozen_manifests["harness"]["canonical_harness_sha256"],
    "source_bytes_unchanged_during_run": runtime_manifest() == frozen_manifests["source"],
    "harness_bytes_unchanged_during_run": harness_manifest() == frozen_manifests["harness"],
    "source_import_path": str(workspace / "src"), "python": sys.version,
    "package_versions": {name: importlib.metadata.version(name) for name in
        ("pytest", "pydantic", "httpx", "fastapi", "psutil", "numpy", "mealpy", "streamlit", "protego", "pypdf")},
    "junit_counts": totals, "log": str(log_path.relative_to(evidence)),
    "junit": str(junit_path.relative_to(evidence)),
    "guard_report": str(guard_path.relative_to(evidence)),
    "live_provider_selected": False, "real_model_call_budget": 0,
    "numeric_execution_scope": "Only selected offline tests; no separate solve or campaign",
}
selection_counts = re.findall(r"(\d+) deselected", log_path.read_text(encoding="utf-8"))
result["deselected"] = int(selection_counts[-1]) if selection_counts else 0
result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print("RUN_RESULT", json.dumps(result), flush=True)
raise SystemExit(code)
