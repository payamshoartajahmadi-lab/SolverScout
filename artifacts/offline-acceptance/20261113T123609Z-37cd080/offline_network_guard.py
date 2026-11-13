"""Offline-run safety guard: allow loopback sockets only in the pytest process.

This is execution evidence tooling, not an application feature or a test result.
Provider fixtures use MockTransport; numeric child processes have their own
application isolation and no provider configuration. No address values are logged.
"""
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_blocked = 0


def _check(address, family):
    global _blocked
    if family not in (socket.AF_INET, socket.AF_INET6):
        return
    host = address[0]
    if host == "localhost":
        return
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        _blocked += 1
        raise OSError("Offline test run prohibits non-loopback network connections")


def _connect(sock, address):
    _check(address, sock.family)
    return _original_connect(sock, address)


def _connect_ex(sock, address):
    _check(address, sock.family)
    return _original_connect_ex(sock, address)


def pytest_sessionstart(session):
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex


def pytest_runtest_logreport(report):
    """Persist progress without test values, credentials or request payloads."""
    output = os.environ.get("SOLVERSCOUT_OFFLINE_GUARD_REPORT")
    if output and (report.when == "call" or report.failed):
        progress = Path(output.replace("-network-guard.json", "-progress.jsonl"))
        with progress.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"nodeid": report.nodeid, "phase": report.when,
                                     "outcome": report.outcome, "duration_seconds": report.duration}) + "\n")


def pytest_sessionfinish(session, exitstatus):
    socket.socket.connect = _original_connect
    socket.socket.connect_ex = _original_connect_ex
    output = os.environ.get("SOLVERSCOUT_OFFLINE_GUARD_REPORT")
    if output:
        Path(output).write_text(json.dumps({
            "guard": "pytest process and its threads: only loopback sockets allowed",
            "blocked_non_loopback_connection_attempts": _blocked,
            "pytest_exitstatus": int(exitstatus),
            "live_provider_test_selected": any(
                item.get_closest_marker("live_provider") is not None
                for item in session.items
            ),
            "real_model_calls_authorized": 0,
            "application_import_path": getattr(sys.modules.get("p_mc_optimizer"), "__file__", None),
        }, indent=2) + "\n", encoding="utf-8")
