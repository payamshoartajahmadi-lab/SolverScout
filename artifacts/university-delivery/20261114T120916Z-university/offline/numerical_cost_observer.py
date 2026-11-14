"""Read-only pytest cost observer; no altered budgets, seeds or results."""
import functools
import json
import os
from pathlib import Path
from threading import Lock

_parent = os.getpid()
_lock = Lock()
_counts = {"supervised_execution_returns": 0, "supervised_known_started": 0,
           "supervised_unknown_reserved": 0, "in_process_known_started": 0,
           "supervised_raised_without_result": 0}


def pytest_configure(config):
    from p_mc_optimizer.execution import supervisor
    from p_mc_optimizer.execution.evaluator import BudgetedEvaluator
    real_run = supervisor.run_execution
    real_call = BudgetedEvaluator.__call__

    @functools.wraps(real_run)
    def observed_run(*args, **kwargs):
        try:
            result = real_run(*args, **kwargs)
        except BaseException:
            if os.getpid() == _parent:
                with _lock:
                    _counts["supervised_raised_without_result"] += 1
            raise
        if os.getpid() == _parent:
            with _lock:
                _counts["supervised_execution_returns"] += 1
                _counts["supervised_known_started"] += result.started_evaluations
                if not result.history_complete:
                    request = args[0] if args else kwargs.get("request")
                    _counts["supervised_unknown_reserved"] += max(0, request.evaluation_quota-result.started_evaluations)
        return result

    @functools.wraps(real_call)
    def observed_call(evaluator, *args, **kwargs):
        before = evaluator.started
        try:
            return real_call(evaluator, *args, **kwargs)
        finally:
            if os.getpid() == _parent:
                with _lock:
                    _counts["in_process_known_started"] += max(0, evaluator.started-before)

    supervisor.run_execution = observed_run
    BudgetedEvaluator.__call__ = observed_call


def pytest_sessionfinish(session, exitstatus):
    result = {"scope": "Offline regression only, separate from acceptance/preparation/smoke",
              "observed": _counts, "exit_status": int(exitstatus),
              "observed_known_started_lower_bound": _counts["supervised_known_started"]+_counts["in_process_known_started"],
              "coverage": "Parent-process evaluator starts and completed real supervisor calls only; direct custom subprocess tests or raised calls may not be measured. This is not claimed as a complete physical FE total.",
              "budgets_seeds_thresholds_changed": False}
    Path(os.environ["SOLVERSCOUT_TEST_USAGE_PATH"]).write_text(json.dumps(result,indent=2),encoding="utf-8")
