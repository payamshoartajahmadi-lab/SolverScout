"""One owner per data directory; the instance token travels only in child environments."""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx
import psutil
from filelock import FileLock, Timeout

from p_mc_optimizer.config import LocalConfig, config_json


class LocalRuntimeError(RuntimeError):
    pass


def runtime_dir(data_dir):
    path = data_dir / "runtime"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def owner(data_dir):
    try:
        info = json.loads((runtime_dir(data_dir) / "instance.json").read_text(encoding="utf-8"))
        proc = psutil.Process(info["pid"])
        if (proc.status() != psutil.STATUS_ZOMBIE and abs(proc.create_time() - info["created"]) < 0.01 and
                "p_mc_optimizer" in proc.cmdline() and info["instance_id"] in proc.cmdline()):
            return info
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return None
    return None


def health(url, *, instance_id=None):
    try:
        with httpx.Client(timeout=0.75, trust_env=False) as client:
            response = client.get(url)
            return response.status_code == 200 and (instance_id is None or
                   response.json().get("instance_id") == instance_id)
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def ensure_port(port):
    with socket.socket() as sock:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise LocalRuntimeError(f"PORT_IN_USE: پورت {port} در دسترس نیست.") from None


def instance_lock(data_dir):
    return FileLock(str(runtime_dir(data_dir) / "instance.lock"), timeout=0)


def _wait_ready(data_dir, config, proc, *, open_browser):
    for _ in range(180):
        if proc is not None and proc.poll() is not None:
            raise LocalRuntimeError("START_FAILED: جزئیات در پوشهٔ logs دادهٔ محلی است.")
        info = owner(data_dir)
        if (info and health(config.api_url + "/healthz", instance_id=info["instance_id"]) and
                health(config.ui_url + "/_stcore/health")):
            if open_browser:
                webbrowser.open(config.ui_url)
            print(config.ui_url)
            return
        time.sleep(0.2)
    raise LocalRuntimeError("START_TIMEOUT: سرویس‌ها در مهلت راه‌اندازی آماده نشدند.")


def start(data_dir, config, *, open_browser=True):
    runtime = runtime_dir(data_dir)
    proc = None
    with FileLock(str(runtime / "start.lock"), timeout=35):
        existing = owner(data_dir)
        if existing:
            config = LocalConfig.model_validate(existing["config"])
        else:
            try:
                with instance_lock(data_dir):
                    ensure_port(config.api_port)
                    ensure_port(config.ui_port)
            except Timeout:
                raise LocalRuntimeError("INSTANCE_ALREADY_RUNNING: این مسیر داده در حال استفاده است.") from None
            instance_id = str(uuid4())
            logs = data_dir / "logs"
            logs.mkdir(exist_ok=True, mode=0o700)
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
            with (logs / "launcher.log").open("ab") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "p_mc_optimizer", "_supervise", str(data_dir),
                     config_json(config), instance_id],
                    env=child_environment(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=os.name != "nt", creationflags=flags,
                )
        _wait_ready(data_dir, config, proc, open_browser=open_browser)


def stop(data_dir):
    info = owner(data_dir)
    if info is None:
        print("نمونهٔ فعالی برای این مسیر داده یافت نشد.")
        return
    (runtime_dir(data_dir) / f"stop-{info['instance_id']}").touch(mode=0o600)
    for _ in range(150):
        current = owner(data_dir)
        if current is None or current["instance_id"] != info["instance_id"]:
            print("برنامه متوقف شد.")
            return
        time.sleep(0.2)
    raise LocalRuntimeError("STOP_TIMEOUT: نمونه در مهلت مورد انتظار متوقف نشد.")


def child_environment():
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE",
        "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "XDG_RUNTIME_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "DBUS_SESSION_BUS_ADDRESS",
        "USER", "USERNAME", "LOGNAME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
    }
    values = {key: val for key, val in os.environ.items() if key.upper() in allowed}
    values.update({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "LANGSMITH_TRACING": "false",
                   "LANGCHAIN_TRACING_V2": "false", "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false"})
    return values



def watch_supervisor():
    """Reap services after abrupt launcher loss, including an unopened UI."""
    if "PMC_SUPERVISOR_PID" not in os.environ:
        return
    pid = int(os.environ["PMC_SUPERVISOR_PID"])
    created = float(os.environ["PMC_SUPERVISOR_CREATED"])

    def watch():
        while True:
            try:
                parent = psutil.Process(pid)
                if abs(parent.create_time() - created) >= 0.01 or parent.status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(0.25)
        if os.name != "nt":
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
        os._exit(4)

    Thread(target=watch, daemon=True).start()


def _reap(proc, *, timeout=5):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def supervise(data_dir, config, instance_id):
    runtime = runtime_dir(data_dir)
    with instance_lock(data_dir):
        ensure_port(config.api_port)
        ensure_port(config.ui_port)
        owned = runtime / "instance.json"
        marker = runtime / f"stop-{instance_id}"
        env = child_environment()
        env.update({"PMC_INTERNAL_TOKEN": secrets.token_urlsafe(32), "PMC_API_URL": config.api_url,
                    "PMC_DATA_DIR": str(data_dir), "PMC_WORKER_STOP": str(marker), "PMC_INSTANCE_ID": instance_id})
        info = {"instance_id": instance_id, "pid": os.getpid(),
                "created": psutil.Process().create_time(), "config": config.model_dump()}
        env.update({"PMC_SUPERVISOR_PID": str(info["pid"]), "PMC_SUPERVISOR_CREATED": str(info["created"])})
        tmp = runtime / f"{instance_id}.part"
        tmp.write_text(json.dumps(info), encoding="utf-8")
        tmp.replace(owned)
        done = False

        def finish(*_):
            nonlocal done
            done = True

        signal.signal(signal.SIGTERM, finish)
        signal.signal(signal.SIGINT, finish)
        log_dir = data_dir / "logs"
        log_dir.mkdir(exist_ok=True, mode=0o700)
        children = {}
        python = sys.executable
        commands = {
            "api": [python, "-m", "p_mc_optimizer", "_api", str(config.api_port)],
            "worker": [python, "-m", "p_mc_optimizer", "_worker"],
            "ui": [python, "-m", "p_mc_optimizer", "_ui", "run", str(Path(__file__).parent / "ui" / "app.py"),
                   "--server.address=127.0.0.1", f"--server.port={config.ui_port}",
                   "--server.headless=true", "--server.enableXsrfProtection=true",
                   "--server.maxUploadSize=1", "--server.fileWatcherType=none",
                   "--browser.gatherUsageStats=false"],
        }
        try:
            for role, command in commands.items():
                with (log_dir / f"{role}.log").open("ab") as log:
                    children[role] = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                                      stdout=log, stderr=log)
                if role == "api":
                    for _ in range(150):
                        if done or marker.exists() or children[role].poll() is not None:
                            raise LocalRuntimeError("START_FAILED: راه‌اندازی Backend متوقف شد.")
                        if health(config.api_url + "/healthz", instance_id=instance_id):
                            break
                        time.sleep(0.2)
                    else:
                        raise LocalRuntimeError("START_TIMEOUT: Backend آماده نشد.")
            while not done and not marker.exists():
                if any(proc.poll() is not None for proc in children.values()):
                    raise LocalRuntimeError("CHILD_EXITED: یکی از سرویس‌های محلی پایان یافت.")
                time.sleep(0.2)
        finally:
            marker.touch(mode=0o600, exist_ok=True)
            if "worker" in children:
                try:
                    children["worker"].wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _reap(children["worker"], timeout=3)
            for role in ("ui", "api"):
                if role in children:
                    _reap(children[role])
            owned.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
