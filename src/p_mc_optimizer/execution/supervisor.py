"""Isolated, deadline-bound numerical execution and durable history."""

from __future__ import annotations

import hashlib
import multiprocessing as mp
import os
import platform
import traceback
from collections.abc import Callable
from multiprocessing.connection import wait
from pathlib import Path
from threading import Lock, Thread
from time import monotonic

import numpy as np

from p_mc_optimizer.domain.models import (
    PREFLIGHT_EVALUATIONS,
    PROFILE_EVALUATIONS,
    VERIFICATION_EVALUATIONS,
    DomainError,
    EvaluationRecord,
    ExecutionError,
    ExecutionRequest,
    ExecutionResult,
)
from p_mc_optimizer.execution.evaluator import (
    BudgetedEvaluator,
    EvaluationBudgetExhausted,
    ExecutionCancelled,
    ExecutionTimedOut,
)
from p_mc_optimizer.problems.registry import objective_for, problem_hashes
from p_mc_optimizer.profiling.stages import (
    profile_features,
    run_preflight,
    run_profile,
    run_verification,
    validation_result,
)
from p_mc_optimizer.solvers.registry import run_solver


_GRACE_SECONDS = 0.10
_POLL_SECONDS = 0.025
_SPAWN_LOCK = Lock()
_CHILD_ENV_KEYS = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "PYTHONUTF8", "PYTHONIOENCODING",
})
_CHILD_ENV_FIXED = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "MPLBACKEND": "Agg",
}
_PHASE_FUNCTIONS = {
    "preflight": run_preflight,
    "profile": run_profile,
    "verification": run_verification,
}
_STAGE_QUOTAS = {
    "preflight": PREFLIGHT_EVALUATIONS,
    "profile": PROFILE_EVALUATIONS,
    "verification": VERIFICATION_EVALUATIONS,
}


def _start_isolated(process) -> None:
    # multiprocessing.spawn has no per-child env argument. Set a positive allowlist
    # only for creation, then restore the worker environment before processing IPC.
    with _SPAWN_LOCK:
        original = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update({key: value for key, value in original.items() if key.upper() in _CHILD_ENV_KEYS})
            os.environ.update(_CHILD_ENV_FIXED)
            process.start()
        finally:
            os.environ.clear()
            os.environ.update(original)


def _exit_if_parent_dies() -> None:
    parent = mp.parent_process()
    if parent is not None:
        wait([parent.sentinel])
        os._exit(86)


def _worker(
    request_json: str,
    channel,
    stop_event,
    deadline: float,
    objective_override: Callable | None,
) -> None:
    Thread(target=_exit_if_parent_dies, name="numeric-parent-watchdog", daemon=True).start()
    evaluator = None
    status, reason = "completed", "solver_stop"
    error = None
    diagnostic = None
    try:
        request = ExecutionRequest.model_validate_json(request_json)
        revision = request.revision
        if problem_hashes(revision.spec) != (revision.problem_math_hash, revision.evaluation_contract_hash):
            raise DomainError("EVALUATION_CONTRACT_MISMATCH", "اثر انگشت مدل یا ارزیاب با snapshot مطابقت ندارد.")

        def request_start(sequence: int) -> None:
            channel.send(("started", sequence))
            permission = channel.recv()
            if permission == "cancelled":
                raise ExecutionCancelled
            if permission == "timed_out":
                raise ExecutionTimedOut

        def send_record(record: EvaluationRecord) -> None:
            channel.send(("result", record.model_dump(mode="json")))

        evaluator = BudgetedEvaluator(
            request,
            objective_override if objective_override is not None else objective_for(revision.spec),
            deadline=deadline,
            should_cancel=stop_event.is_set,
            start=request_start,
            record=send_record,
        )
        operation = _PHASE_FUNCTIONS.get(request.phase, run_solver)
        operation(evaluator)
        if evaluator.started == request.evaluation_quota:
            reason = "evaluation_budget"
    except EvaluationBudgetExhausted:
        reason = "evaluation_budget"
    except ExecutionCancelled:
        status, reason = "cancelled", "cancelled"
    except ExecutionTimedOut:
        status, reason = "timed_out", "time_budget"
    except DomainError as exc:
        status, reason = "failed", "execution_error"
        cause = exc.__cause__ or exc
        error = ExecutionError(
            error_code=exc.code,
            message_fa=str(exc),
            technical_type=type(cause).__name__,
            technical_message=str(cause),
        )
        diagnostic = traceback.format_exc()
    except Exception as exc:
        status, reason = "failed", "execution_error"
        error = ExecutionError(
            error_code="SOLVER_EXECUTION_FAILED",
            message_fa="اجرای عددی به علت خطای داخلی متوقف شد.",
            technical_type=type(exc).__name__,
            technical_message=str(exc),
        )
        diagnostic = traceback.format_exc()
    try:
        channel.send(("done", {
            "execution_status": status,
            "termination_reason": reason,
            "started_evaluations": evaluator.started if evaluator else 0,
            "successful_evaluations": evaluator.successful if evaluator else 0,
            "failed_evaluations": evaluator.failed if evaluator else 0,
            "rejected_proposals": evaluator.rejected if evaluator else 0,
            "error": error.model_dump(mode="json") if error else None,
            "diagnostic": diagnostic,
        }))
    finally:
        channel.close()


def _join(process, *, force: bool = False) -> None:
    if force and process.is_alive():
        process.terminate()
    process.join(_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join()


def _fingerprint(request: ExecutionRequest) -> dict[str, str]:
    result = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "dtype": "float64",
        "rng": "PCG64",
    }
    if request.trial is not None:
        result.update(mealpy=request.trial.library_version, adapter=request.trial.adapter_version)
    return result


def run_execution(
    request: ExecutionRequest,
    *,
    history_path: Path,
    cancelled: Callable[[], bool] = lambda: False,
    on_event: Callable[[dict], bool | None] | None = None,
    _objective_override: Callable | None = None,
) -> ExecutionResult:
    """Run a single immutable execution; acknowledge durable starts before objective calls.

    The private callable argument is only for trusted Python fault/spy fixtures.
    API inputs and persisted jobs never carry callables.
    """
    if request.phase in _STAGE_QUOTAS and request.evaluation_quota != _STAGE_QUOTAS[request.phase]:
        raise DomainError("INVALID_EXECUTION_QUOTA", "سهم مرحله با قرارداد نسخهٔ اول مطابقت ندارد.")

    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = history_path.with_name(history_path.name + ".part")
    if history_path.exists():
        raise FileExistsError("Numeric execution already has a history artifact")

    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    stop_event = context.Event()
    started_at = monotonic()
    deadline = started_at + request.deadline_seconds
    process = context.Process(
        target=_worker,
        args=(request.model_dump_json(), child, stop_event, deadline, _objective_override),
        daemon=True,
        name=f"p-mc-numeric-{request.execution_id}",
    )
    stop_status = None
    stop_deadline = None
    next_cancel_check = started_at
    done = None
    started = succeeded = failed = 0
    best = None
    sampled = []
    protocol_error = False
    digest = hashlib.sha256()
    error = None
    try:
        with partial_path.open("xb") as history:
            _start_isolated(process)
            child.close()
            while True:
                now = monotonic()
                cancel_requested = False
                if stop_status is None and now >= next_cancel_check:
                    cancel_requested = cancelled()
                    next_cancel_check = now + _POLL_SECONDS
                if stop_status is None and (cancel_requested or now >= deadline):
                    stop_status = "cancelled" if cancel_requested else "timed_out"
                    stop_deadline = now + _GRACE_SECONDS
                    stop_event.set()
                if stop_deadline is not None and now >= stop_deadline:
                    break
                next_deadline = stop_deadline if stop_deadline is not None else deadline
                if not parent.poll(min(_POLL_SECONDS, max(0.0, next_deadline - now))):
                    if not process.is_alive():
                        break
                    continue
                try:
                    event_type, payload = parent.recv()
                except (EOFError, OSError):
                    break
                if event_type == "started":
                    if payload != started + 1 or payload > request.evaluation_quota or succeeded + failed != started:
                        protocol_error = True
                        break
                    if stop_status is not None:
                        parent.send(stop_status)
                        continue
                    if on_event and on_event({"kind": "started", "sequence": payload}) is False:
                        stop_status = "cancelled"
                        stop_deadline = monotonic() + _GRACE_SECONDS
                        stop_event.set()
                        parent.send(stop_status)
                        continue
                    started = payload
                    parent.send("accepted")
                elif event_type == "result":
                    record = EvaluationRecord.model_validate(payload)
                    if (
                        record.sequence != started
                        or record.sequence != succeeded + failed + 1
                        or record.job_id != request.job_id
                        or record.execution_id != request.execution_id
                        or record.phase != request.phase
                        or record.problem_math_hash != request.revision.problem_math_hash
                    ):
                        protocol_error = True
                        break
                    encoded = (record.model_dump_json() + "\n").encode("utf-8")
                    history.write(encoded)
                    history.flush()
                    digest.update(encoded)
                    if record.status == "successful":
                        succeeded += 1
                        if record.phase != "verification" and (
                            best is None or record.score_for_minimization < best.score_for_minimization
                        ):
                            best = record
                    else:
                        failed += 1
                    if request.phase in {"profile", "verification"}:
                        sampled.append(record)
                    if on_event:
                        on_event({"kind": "result", "record": payload})
                elif event_type == "done":
                    done = payload
                    break
                else:
                    protocol_error = True
                    break
            history.flush()
            os.fsync(history.fileno())
    finally:
        child.close()
        if process.pid is not None:
            _join(process, force=done is None)
            process.close()
        parent.close()
    elapsed = monotonic() - started_at

    history_complete = (
        done is not None
        and not protocol_error
        and (started, succeeded, failed) == (
            done["started_evaluations"], done["successful_evaluations"], done["failed_evaluations"]
        )
        and started == succeeded + failed
    )
    if history_complete:
        os.replace(partial_path, history_path)
        if done["diagnostic"]:
            history_path.with_name(history_path.name + ".traceback.txt").write_text(done["diagnostic"], encoding="utf-8")
    if stop_status == "cancelled":
        status, reason = "cancelled", "cancelled"
    elif stop_status == "timed_out":
        status, reason = "timed_out", "time_budget"
    elif not history_complete:
        status, reason = "unknown_outcome", "execution_error"
        error = ExecutionError(error_code="NUMERIC_OUTCOME_UNKNOWN", message_fa="پایان اجرای عددی و تاریخچهٔ آن قابل تطبیق نیست.")
    else:
        status, reason, error = done["execution_status"], done["termination_reason"], done["error"]

    profile = None
    validation = None
    if request.phase == "profile" and status == "completed" and succeeded == PROFILE_EVALUATIONS:
        profile = profile_features(request, sampled)
    if request.phase == "verification":
        validation = validation_result(request, sampled, status)
        if validation.validation_status == "failed" and status == "completed":
            reason = "validation_error"
    return ExecutionResult(
        job_id=request.job_id,
        execution_id=request.execution_id,
        phase=request.phase,
        execution_status=status,
        termination_reason=reason,
        best=best,
        started_evaluations=started,
        successful_evaluations=succeeded,
        failed_evaluations=failed,
        rejected_proposals=done["rejected_proposals"] if done else 0,
        elapsed_compute_seconds=elapsed,
        overshoot_seconds=max(0.0, elapsed - request.deadline_seconds),
        history_complete=history_complete,
        history_sha256=digest.hexdigest() if history_complete else None,
        profile_features=profile,
        validation=validation,
        error=error,
        fingerprint=_fingerprint(request),
    )
