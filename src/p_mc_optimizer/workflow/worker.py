"""Single job claim loop with lease heartbeats."""

from __future__ import annotations

import signal
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from p_mc_optimizer.workflow.graph import Workflow


def run_claimed(repo, job, worker_id, *, api_url=None, token=None, stopped=lambda: False):
    done = Event()
    lost = Event()
    job_id = job["job_id"]

    def heartbeat():
        while not done.wait(4):
            if not repo.renew_lease(job_id, worker_id):
                lost.set()
                return

    pulse = Thread(target=heartbeat, daemon=True)
    pulse.start()
    workflow_type = Workflow
    version = job["policy"]["policy_version"]
    if version == "2.1":
        from p_mc_optimizer.workflow.library_v21 import LibraryWorkflow

        workflow_type = LibraryWorkflow
    elif version == "2.0":
        from p_mc_optimizer.workflow.catalog_v2 import CatalogWorkflow

        workflow_type = CatalogWorkflow
    workflow = workflow_type(repo, job_id, worker_id, api_url=api_url, token=token,
                             shutdown=lambda: stopped() or lost.is_set())
    try:
        workflow.run()
    except Exception as exc:
        if lost.is_set() or stopped():
            return
        if any(row["execution_status"] == "running" for row in repo.list_executions(job_id)):
            # The supervisor reaps its child before propagating an exception.
            # Reconcile the durable reservation without retrying that execution.
            repo.recover_expired_jobs(force=True)
            return
        # Diagnostics retain only the exception class. Provider and user payloads stay out of logs.
        code = getattr(exc, "error_code", None) or getattr(exc, "code", None) or "EXECUTION_ERROR"
        workflow.event("workflow_error", error_code=code, exception_type=type(exc).__name__)
        workflow.stop(code, "گردش‌کار با خطای اجرایی متوقف شد.")
        workflow.build_report_data()
        workflow.explain_result()
        workflow.finalize_job()
        if api_url is None:
            raise
    finally:
        done.set()
        pulse.join(timeout=2)


def serve(repo, *, api_url, token, stop_file: Path | None):
    stop = Event()
    worker_id = str(uuid4())

    def stopped():
        return stop.is_set() or (stop_file is not None and stop_file.exists())

    def end(*_):
        stop.set()

    signal.signal(signal.SIGTERM, end)
    signal.signal(signal.SIGINT, end)
    repo.recover_expired_jobs(force=True)
    while not stopped():
        repo.recover_expired_jobs()
        job = repo.claim_job(worker_id)
        if job:
            run_claimed(repo, job, worker_id, api_url=api_url, token=token, stopped=stopped)
        else:
            stop.wait(0.25)
