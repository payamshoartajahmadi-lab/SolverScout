"""Public launcher, diagnostics, and shared-engine headless commands."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import sys
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from filelock import Timeout

from p_mc_optimizer import __version__
from p_mc_optimizer.config import LocalConfig, data_directory, load_config
from p_mc_optimizer.persistence.migrations import MigrationError
from p_mc_optimizer.launcher import LocalRuntimeError, health, instance_lock, owner, start, stop, supervise, watch_supervisor


def _policy(args):
    return {"policy_version": "1.0", "portfolio_id": "continuous_v1",
            "selection_policy": args.policy, "fixed_algorithm_id": args.solver,
            "max_objective_evaluations": args.budget, "max_compute_seconds": args.seconds,
            "master_seed": args.seed, "explain_via_llm": False}


def solve(repo, problem, policy):
    from p_mc_optimizer.application.jobs import ApplicationService
    from p_mc_optimizer.domain.models import CreateJob
    from p_mc_optimizer.workflow.worker import run_claimed

    request = CreateJob.model_validate({
        "input": {"kind": "structured_problem", "problem": problem},
        "policy": policy, "llm_profile": None,
    })
    service = ApplicationService(repo)
    created = service.create_job(request, str(uuid4()))
    worker_id = str(uuid4())
    repo.recover_expired_jobs(force=True)
    while claimed := repo.claim_job(worker_id, job_id=created["job_id"]):
        run_claimed(repo, claimed, worker_id)
    return repo.get_report(created["job_id"])


def doctor(data_dir, config):
    from p_mc_optimizer.persistence import Repository
    from p_mc_optimizer.persistence.migrations import fts5_available

    repo = Repository(data_dir)
    with repo.connect() as connection:
        schema = connection.execute("PRAGMA user_version").fetchone()[0]
        index_available = fts5_available(connection) and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_fts'"
        ).fetchone() is not None
    from keyring import get_keyring
    import protego
    import pypdf
    import pyarrow  # Detect the NumPy/Arrow ABI combination during setup.
    try:
        backend = get_keyring()
        keyring_backend = f"{type(backend).__module__}.{type(backend).__name__}"
    except Exception:
        keyring_backend = "unavailable"
    result = {
        "core": "PASS", "project_version": __version__, "python": platform.python_version(),
        "os": platform.platform(), "arch": platform.machine(), "sqlite": sqlite3.sqlite_version,
        "database_schema": schema, "data_dir": str(data_dir),
        "local_index": "PASS" if index_available else "LOCAL_INDEX_UNAVAILABLE",
        "search": "TAVILY_ADAPTER_AVAILABLE_CONNECTION_NOT_CHECKED",
        "api_url": config.api_url, "ui_url": config.ui_url,
        "api_running": health(config.api_url + "/healthz"),
        "instance_running": owner(data_dir) is not None, "llm": "NOT_CONFIGURED",
        "keyring_backend": keyring_backend, "pyarrow": pyarrow.__version__,
        "versions": {name: version(name) for name in
                     ["fastapi", "langgraph", "mealpy", "numpy", "pydantic", "streamlit", "keyring", "pypdf", "protego"]},
    }
    with repo.connect() as connection:
        if connection.execute("SELECT 1 FROM provider_profiles LIMIT 1").fetchone():
            result["llm"] = "PROFILE_SAVED_CONNECTION_NOT_CHECKED"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def rebuild_library_index(repo):
    from p_mc_optimizer.persistence.migrations import execute_sql, fts5_available
    from p_mc_optimizer.research.library_schema import LIBRARY_FTS_SCHEMA
    from p_mc_optimizer.research.library_store import LibraryStore, normalize_index_text
    from p_mc_optimizer.research.storage import ResearchStore

    library = LibraryStore(ResearchStore(repo.path, repo.data_dir / "artifacts" / "research"))
    indexed = skipped = 0
    with repo.transaction() as connection:
        if not fts5_available(connection):
            raise MigrationError("LOCAL_INDEX_UNAVAILABLE: SQLite FTS5 is required")
        connection.execute("DROP TABLE IF EXISTS library_fts")
        execute_sql(connection, LIBRARY_FTS_SCHEMA)
        versions = connection.execute("SELECT rowid,version_id,payload FROM library_versions ORDER BY rowid")
        for row in versions:
            version = json.loads(row["payload"])
            text = library.verify_artifacts(version)
            if text is None:
                skipped += 1
                continue
            connection.execute(
                "INSERT INTO library_fts(rowid,version_id,title,body) VALUES(?,?,?,?)",
                (row["rowid"], row["version_id"], normalize_index_text(version["title"]), normalize_index_text(text)),
            )
            indexed += 1
    print(json.dumps({"indexed_versions": indexed, "skipped_invalid_versions": skipped}))
    return 0


def parser():
    root = argparse.ArgumentParser(prog="p_mc_optimizer")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--data-dir", type=Path)
    root.add_argument("--config", type=Path)
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("doctor", "start", "stop", "solve", "benchmark", "package", "migrate", "library-reindex"):
        part = sub.add_parser(command)
        part.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS)
        part.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if command == "start":
            part.add_argument("--no-browser", action="store_true")
        elif command == "solve":
            part.add_argument("--problem", required=True, type=Path)
            part.add_argument("--policy", default="fixed_solver",
                              choices=["fixed_solver", "random_solver", "rule_based", "pilot_only"])
            part.add_argument("--solver")
            part.add_argument("--budget", type=int, default=20000)
            part.add_argument("--seconds", type=int, default=300)
            part.add_argument("--seed", type=int, default=1405)
        elif command == "benchmark":
            part.add_argument("--manifest", required=True, type=Path)
        elif command == "package":
            part.add_argument("--output", required=True, type=Path)
    return root


def internal(args):
    if args[0] == "_supervise":
        supervise(Path(args[1]), LocalConfig.model_validate_json(args[2]), args[3])
        return
    watch_supervisor()
    if args[0] == "_ui":
        from streamlit.web.cli import main as streamlit_main

        sys.argv = ["streamlit", *args[1:]]
        streamlit_main()
        return
    directory = Path(os.environ["PMC_DATA_DIR"])
    token = os.environ["PMC_INTERNAL_TOKEN"]
    if args[0] == "_api":
        import uvicorn
        from p_mc_optimizer.api import create_app

        uvicorn.run(create_app(directory, token, instance_id=os.environ.get("PMC_INSTANCE_ID")),
                    host="127.0.0.1", port=int(args[1]), workers=1, access_log=False, timeout_graceful_shutdown=4)
    elif args[0] == "_worker":
        from p_mc_optimizer.persistence import Repository
        from p_mc_optimizer.workflow.worker import serve

        serve(Repository(directory), api_url=os.environ["PMC_API_URL"], token=token,
              stop_file=Path(os.environ["PMC_WORKER_STOP"]))
    else:
        raise ValueError("فرمان داخلی نامعتبر است.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0].startswith("_"):
            internal(argv)
            return 0
        args = parser().parse_args(argv)
        directory = data_directory(args.data_dir)
        config = load_config(args.config)
        if args.command == "start":
            start(directory, config, open_browser=not args.no_browser)
            return 0
        if args.command == "stop":
            stop(directory)
            return 0
        if args.command == "doctor":
            return doctor(directory, config)
        if args.command == "package":
            from p_mc_optimizer.release import build_zip

            root = Path(__file__).resolve().parents[2]
            print(build_zip(root, args.output))
            return 0
        from p_mc_optimizer.domain.models import strict_json_loads
        from p_mc_optimizer.persistence import Repository

        with instance_lock(directory):
            repo = Repository(directory, allow_upgrade=args.command == "migrate")
            if args.command == "migrate":
                print("SQLite schema is ready.")
                return 0
            if args.command == "library-reindex":
                return rebuild_library_index(repo)
            if args.command == "benchmark":
                from p_mc_optimizer.benchmarks import run_benchmark
                result = run_benchmark(repo, args.manifest, solve)
                print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
                return 0 if result["failed_runs"] == 0 else 4
            data = strict_json_loads(args.problem.read_bytes(), limit=256 * 1024)
            if args.policy == "fixed_solver" and args.solver is None:
                args.solver = "de.original"
            result = solve(repo, data, _policy(args))
            report = result["report"]
            print(json.dumps({k: report.get(k) for k in
                              ["job_id", "job_status", "solution_status", "optimality_status", "best_x",
                               "objective_raw", "selected_algorithm_id", "budget", "error"]} |
                             {"artifacts": [str(repo.artifact_path(a["artifact_id"])) for a in result["artifacts"]]},
                             ensure_ascii=False, indent=2, allow_nan=False))
            return 0 if report["job_status"] == "completed" and report["solution_status"] == "valid_best_found" else 4
    except KeyboardInterrupt:
        return 130
    except Timeout:
        print("INSTANCE_ALREADY_RUNNING: از رابط فعال یا یک --data-dir دیگر استفاده کنید.", file=sys.stderr)
        return 3
    except (LocalRuntimeError, MigrationError, OSError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except ValueError as exc:
        print(str(exc).splitlines()[0], file=sys.stderr)
        return 2
    except Exception as exc:
        code = getattr(exc, "error_code", "EXECUTION_ERROR")
        print(f"{code}: {getattr(exc, 'message_fa', type(exc).__name__)}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
