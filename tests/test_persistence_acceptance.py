"""TEST FIXTURE. Repository and HTTP acceptance tests without provider traffic."""
from __future__ import annotations

import copy
import hashlib
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import psutil
import pytest
from fastapi.testclient import TestClient

from p_mc_optimizer.api import create_app
from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.application.jobs import ApplicationService, StructuredCompletion, TextualAnswers
from p_mc_optimizer.domain.models import CreateJob
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.problems.registry import freeze_problem

TEST_TOKEN = "TEST_FIXTURE_LOCAL_TOKEN_7b73b48905264789ab6c4bc6773b7f24"
TEST_HEADERS = {"X-PMC-Internal-Token": TEST_TOKEN, "Idempotency-Key": "TEST_FIXTURE_SUBMIT_1"}
PROBLEM = {
    "schema_version": "1.0",
    "variables": [
        {"name": name, "label_fa": None, "type": "real", "lower": -5.0, "upper": 5.0, "unit": None}
        for name in ("x1", "x2")
    ],
    "objective": {
        "sense": "minimize",
        "unit": None,
        "source": {"kind": "expression", "expression": "(x1-2)**2+(x2+1)**2"},
    },
    "parameters": {},
    "constraints": [],
    "deterministic": True,
}


@pytest.fixture
def payload():
    return {
        "input": {"kind": "structured_problem", "problem": copy.deepcopy(PROBLEM)},
        "policy": {
            "policy_version": "1.0", "portfolio_id": "continuous_v1", "selection_policy": "fixed_solver",
            "fixed_algorithm_id": "de.original", "max_objective_evaluations": 1002, "max_compute_seconds": 30,
            "master_seed": 1405, "explain_via_llm": False,
        },
        "llm_profile": None,
    }


@pytest.fixture
def repository(tmp_path):
    return Repository(tmp_path / "داده آزمون محلی")


@pytest.fixture
def running(repository, payload):
    request = CreateJob.model_validate(payload)
    job = ApplicationService(repository).create_job(request, "TEST_FIXTURE_RUNNING")
    owner = str(uuid4())
    job_id = job["job_id"]
    claimed = repository.claim_job(owner)
    assert claimed["job_id"] == job_id
    repository.freeze_problem(job_id, freeze_problem(request.input.problem), worker_id=owner)
    return job_id, owner


def test_concurrent_idempotency_claim_and_restart(repository, payload):
    request = CreateJob.model_validate(payload)
    service = ApplicationService(repository)

    def submit(_):
        return service.create_job(request, "TEST_FIXTURE_SAME_SUBMIT")["job_id"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        identities = set(pool.map(submit, range(12)))
    assert len(identities) == 1
    restarted = Repository(repository.data_dir)
    assert {row["job_id"] for row in restarted.list_jobs()} == identities
    assert ApplicationService(restarted).create_job(request, "TEST_FIXTURE_SAME_SUBMIT")["job_id"] in identities
    owner = str(uuid4())
    assert restarted.claim_job(owner)["job_id"] in identities
    assert restarted.claim_job(str(uuid4())) is None


def test_budget_reservation_replay_and_history_hash(repository, running):
    job_id, owner = running
    execution_id = str(uuid4())
    execution = repository.start_execution(job_id, "preflight", 8, 5.0, execution_id=execution_id, worker_id=owner)
    assert execution["created"]
    assert execution["reserved_active"] == 8
    duplicate = repository.start_execution(job_id, "preflight", 8, 4.0, worker_id=owner)
    assert duplicate["execution_id"] == execution_id and not duplicate["created"]
    repository.record_started(execution_id, sequence=1, worker_id=owner)
    repository.record_started(execution_id, sequence=1, worker_id=owner)
    assert repository.get_job(job_id)["budget"]["known_started"] == 1
    with pytest.raises(ValueError, match="out of order"):
        repository.record_started(execution_id, sequence=3, worker_id=owner)
    data = b'{"fixture":"TEST FIXTURE"}\n'
    artifact = repository.save_artifact(job_id, "evaluation_history", data, ".jsonl", worker_id=owner)
    result = {
        "execution_status": "completed", "started_evaluations": 1, "history_complete": True,
        "history_sha256": "wrong", "history_artifact_id": artifact["artifact_id"],
    }
    with pytest.raises(ValueError, match="hash differs"):
        repository.finish_execution(execution_id, result, worker_id=owner)
    assert repository.list_executions(job_id)[0]["execution_status"] == "running"
    result["history_sha256"] = hashlib.sha256(data).hexdigest()
    finished = repository.finish_execution(execution_id, result, worker_id=owner)
    assert finished["known_started"] == 1
    assert finished["reserved_active"] == finished["charged_unknown"] == 0
    assert finished["unused"] == 7
    assert repository.artifact_path(artifact["artifact_id"]).read_bytes() == data


def test_process_death_rolls_back_terminal_transition(repository, running):
    job_id, owner = running
    execution = repository.start_execution(job_id, "preflight", 8, 5.0, worker_id=owner)
    execution_id = execution["execution_id"]
    repository.record_started(execution_id, sequence=1, worker_id=owner)
    script = """
import os
import sys
from p_mc_optimizer.persistence import Repository
repo = Repository(sys.argv[1])
repo.write_event = lambda *args, **kwargs: os._exit(47)
repo.finish_execution(sys.argv[2], {
    "execution_status": "failed", "started_evaluations": 1, "history_complete": False,
}, worker_id=sys.argv[3])
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(repository.data_dir), execution_id, owner],
        capture_output=True, timeout=15, check=False,
    )
    assert process.returncode == 47, process.stderr
    restarted = Repository(repository.data_dir)
    before = restarted.list_executions(job_id)[0]
    assert (before["execution_status"], before["known_started"], before["reserved_active"]) == ("running", 1, 7)
    assert not any(item["event_type"] == "execution_finished" for item in restarted.list_events(job_id))

    assert restarted.recover_expired_jobs(force=True) == [job_id]
    after = restarted.list_executions(job_id)[0]
    assert (after["execution_status"], after["known_started"], after["reserved_active"]) == ("unknown_outcome", 1, 0)
    assert after["charged_unknown"] == 7
    assert restarted.get_job(job_id)["job_status"] == "failed"
    assert restarted.claim_job(str(uuid4())) is None
    report = restarted.get_report(job_id)
    assert report["partial"] and not report["complete"]
    assert report["report"]["best_x"] is None
    assert report["report"]["budget"]["charged_unknown"] == 7
    assert report["report"]["diagnostics"]["interrupted_executions"][0]["execution_id"] == execution_id
    assert {artifact["kind"] for artifact in report["artifacts"]} == {"report_json", "report_html", "trials_csv"}


def test_stale_worker_cannot_finalize_after_reclaim(repository, running):
    job_id, owner = running
    assert repository.renew_lease(job_id, owner)
    repository.recover_expired_jobs(force=True)
    new_owner = str(uuid4())
    assert repository.claim_job(new_owner)["job_id"] == job_id
    assert not repository.renew_lease(job_id, owner)
    with pytest.raises(ApiError) as error:
        repository.update_job(job_id, worker_id=owner, phase="reporting")
    assert error.value.error_code == "WORKER_LEASE_LOST"


def test_optimistic_answers_and_structured_completion(repository, payload):
    request = CreateJob.model_validate(payload)
    service = ApplicationService(repository)
    job = service.create_job(request, "TEST_FIXTURE_DRAFT")
    job_id = job["job_id"]
    question = str(uuid4())
    draft = repository.put_draft(job_id, {
        "original_text": "TEST FIXTURE", "ambiguities": [{"question_id": question}],
    })
    pending = repository.update_job(job_id, job_status="needs_input")
    message = {
        "mode": "textual_answers", "expected_job_version": pending["version"],
        "expected_draft_version": draft["draft_version"],
        "answers": [{"question_id": str(uuid4()), "answer": "TEST FIXTURE response"}],
    }
    with pytest.raises(ApiError) as error:
        service.answer_job(job_id, TextualAnswers.model_validate(message))
    assert error.value.status_code == 422
    message["answers"][0]["question_id"] = question
    message["expected_job_version"] -= 1
    with pytest.raises(ApiError) as error:
        service.answer_job(job_id, TextualAnswers.model_validate(message))
    assert error.value.error_code == "STALE_VERSION"
    assert repository.get_problem(job_id)["draft"]["draft_version"] == draft["draft_version"]
    message["expected_job_version"] = pending["version"]
    queued = service.answer_job(job_id, TextualAnswers.model_validate(message))
    assert queued["job_status"] == "queued"
    assert queued["phase"] == "formulating"
    assert queued["answers"] == message["answers"]
    assert queued["version"] > pending["version"]
    pending = repository.update_job(job_id, job_status="needs_input")
    stored_draft = repository.get_problem(job_id)["draft"]
    completed = service.answer_job(job_id, StructuredCompletion.model_validate({
        "mode": "structured_completion", "expected_job_version": pending["version"],
        "expected_draft_version": stored_draft["draft_version"], "problem": PROBLEM,
    }))
    assert completed["job_status"] == "queued"
    assert completed["phase"] == "validating_input"
    assert completed["completion_input"] == PROBLEM
    assert not repository.get_problem(job_id)["frozen"]


def test_active_cancel_blocks_new_execution(repository, running):
    job_id, owner = running
    service = ApplicationService(repository)
    execution_id = repository.start_execution(job_id, "preflight", 8, 5.0, worker_id=owner)["execution_id"]
    assert repository.record_started(execution_id, sequence=1, worker_id=owner)
    job = repository.get_job(job_id)
    status, requested = service.cancel_job(job_id, job["version"])
    assert status == 202 and requested["job_status"] == "running" and requested["cancel_requested"]
    with pytest.raises(ApiError) as error:
        repository.start_execution(job_id, "preflight", 8, 5.0, worker_id=owner)
    assert error.value.error_code == "JOB_CANCEL_REQUESTED"
    assert not repository.record_started(execution_id, sequence=2, worker_id=owner)
    repository.finish_execution(execution_id, {
        "execution_status": "cancelled", "started_evaluations": 1, "history_complete": False,
    }, worker_id=owner)
    terminal = repository.publish_report(
        job_id, {"fixture": "TEST FIXTURE cancelled"}, [], worker_id=owner, job_status="cancelled",
        solution_status="validation_incomplete", explanation_status="not_requested",
        termination_reason="cancelled", partial_result=True,
    )
    assert terminal["job_status"] == "cancelled"
    assert service.cancel_job(job_id, job["version"])[0] == 200
    assert repository.get_report(job_id)["partial"]


def test_http_idempotency_events_download_and_validation(tmp_path, payload):
    app = create_app(tmp_path, TEST_TOKEN)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/jobs").status_code == 401
        initial = client.post("/jobs", json=payload, headers=TEST_HEADERS)
        assert initial.status_code == 202, initial.text
        job = initial.json()
        job_id = job["job_id"]
        duplicate = client.post("/jobs", json=payload, headers=TEST_HEADERS)
        assert duplicate.status_code == 202 and duplicate.json()["job_id"] == job_id
        payload["policy"]["master_seed"] = 91
        conflict = client.post("/jobs", json=payload, headers=TEST_HEADERS)
        assert conflict.status_code == 409 and conflict.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
        assert len(client.get("/jobs", headers=TEST_HEADERS).json()) == 1
        events = client.get(f"/jobs/{job_id}/events", headers=TEST_HEADERS).json()
        assert [row["sequence"] for row in events] == [1]
        assert client.get(f"/jobs/{job_id}/events?after_sequence=1", headers=TEST_HEADERS).json() == []
        assert client.get(f"/jobs/{job_id}/report", headers=TEST_HEADERS).status_code == 409
        assert client.get("/artifacts/not-a-uuid", headers=TEST_HEADERS).status_code == 404
        assert client.get("/artifacts/..%2Fstate.sqlite3", headers=TEST_HEADERS).status_code == 404
        assert client.patch(f"/jobs/{job_id}", json={"budget": 0}, headers=TEST_HEADERS).status_code == 405
        for body in ('{"input": {}, "input": {}}', '{"input": NaN}', '{"input": 1} trailing'):
            invalid = client.post("/jobs", content=body, headers={
                **TEST_HEADERS, "Content-Type": "application/json",
            })
            assert invalid.status_code == 422 and invalid.json()["error_code"] == "INVALID_SCHEMA"
        payload["input"] = {"kind": "natural_language", "text": TEST_TOKEN}
        reflected = client.post("/jobs", json=payload, headers=TEST_HEADERS)
        assert reflected.status_code == 422 and TEST_TOKEN not in reflected.text
        with app.state.repo.connect() as connection:
            stored_key = connection.execute("SELECT idempotency_key FROM idempotency_records").fetchone()[0]
            assert stored_key == hashlib.sha256(TEST_HEADERS["Idempotency-Key"].encode()).hexdigest()
        artifact = app.state.repo.save_artifact(job_id, "report_json", '{"fixture":"TEST FIXTURE"}', ".json")
        downloaded = client.get(f"/artifacts/{artifact['artifact_id']}", headers=TEST_HEADERS)
        assert downloaded.json() == {"fixture": "TEST FIXTURE"}
        assert "report.json" in downloaded.headers["content-disposition"]


def test_start_rejects_unknown_schema_and_preserves_previous_db(tmp_path):
    import sqlite3

    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript("PRAGMA user_version=123; CREATE TABLE previous_user_data (value TEXT);")
        connection.execute("INSERT INTO previous_user_data VALUES ('TEST FIXTURE')")
    with pytest.raises(RuntimeError, match="schema version 123"):
        Repository(tmp_path)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 123
        assert connection.execute("SELECT value FROM previous_user_data").fetchone()[0] == "TEST FIXTURE"


def test_headless_claim_targets_submission_and_queued_cancel_publishes_report(repository, payload):
    request = CreateJob.model_validate(payload)
    service = ApplicationService(repository)
    older = service.create_job(request, "TEST_FIXTURE_OLDER")
    target = service.create_job(request, "TEST_FIXTURE_TARGET")
    owner = str(uuid4())
    assert repository.claim_job(owner, job_id=target["job_id"])["job_id"] == target["job_id"]
    status, cancelled = service.cancel_job(older["job_id"], older["version"])
    assert status == 202 and cancelled["job_status"] == "cancelled"
    report = repository.get_report(older["job_id"])
    assert report["report"]["job_status"] == "cancelled" and report["partial"]
    assert report["report"]["budget"]["known_started"] == 0
    html = next(item for item in report["artifacts"] if item["kind"] == "report_html")
    source = repository.artifact_path(html["artifact_id"]).read_bytes()
    assert hashlib.sha256(source).hexdigest() == html["sha256"]
    assert b'<html lang="fa"' in source
    assert b"<script" not in source


def test_migration_rollback_and_backup_when_legacy_schema_conflicts(tmp_path):
    import sqlite3

    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE jobs (previous_value TEXT)")
        connection.execute("INSERT INTO jobs VALUES ('TEST FIXTURE')")
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        Repository(tmp_path)
    backups = list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    for candidate in [database, *backups]:
        with sqlite3.connect(candidate) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
            assert connection.execute("SELECT previous_value FROM jobs").fetchone()[0] == "TEST FIXTURE"
            assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='problem_revisions'").fetchone()[0] == 0


def test_loopback_worker_death_and_active_cancel_stop_numeric_child(tmp_path, payload):
    """TEST FIXTURE. Actual processes and numeric calls, with zero provider calls."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    stop_path = tmp_path / "worker-stop"
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ, "PMC_DATA_DIR": str(tmp_path), "PMC_INTERNAL_TOKEN": TEST_TOKEN,
        "PMC_API_URL": base_url, "PMC_WORKER_STOP": str(stop_path),
    }
    payload["policy"]["max_objective_evaluations"] = 1_000_000

    def start(*args):
        return subprocess.Popen(
            [sys.executable, "-m", "p_mc_optimizer", *args], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def terminate_fixture(proc):
        # Windows venv launchers add a wrapper process above the real interpreter.
        try:
            descendants = psutil.Process(proc.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        for child in reversed(descendants):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        if proc.poll() is None:
            proc.terminate()
        _, alive = psutil.wait_procs(descendants, timeout=5)
        for child in alive:
            child.kill()
        psutil.wait_procs(alive, timeout=5)
        proc.wait(timeout=5)

    def wait_for(check, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = check()
            except (httpx.HTTPError, psutil.NoSuchProcess):
                result = None
            if result:
                return result
            time.sleep(0.025)
        pytest.fail("Local process fixture did not reach the expected state")

    api = start("_api", str(port))
    worker = None
    try:
        with httpx.Client(base_url=base_url, headers=TEST_HEADERS, trust_env=False, timeout=3) as client:
            wait_for(lambda: client.get("/healthz").is_success)
            worker = start("_worker")
            submission = client.post("/jobs", json=payload)
            assert submission.status_code == 202
            job_id = submission.json()["job_id"]
            route = f"/jobs/{job_id}"

            def active():
                job = client.get(route).json()
                return job if job["phase"] == "solving" and job["budget"]["known_started"] > 500 else None

            first = wait_for(active)
            children = [
                child for child in psutil.Process(worker.pid).children(recursive=True)
                if any("spawn_main" in argument for argument in child.cmdline())
            ]
            assert len(children) == 1
            assert "PMC_INTERNAL_TOKEN" not in children[0].environ()
            # Kill the process that owns the numeric child, not a Windows launcher wrapper.
            worker_process = children[0].parent()
            assert worker_process is not None
            worker_process.kill()
            worker.wait(timeout=5)

            def child_stopped():
                try:
                    return not children[0].is_running() or children[0].status() == psutil.STATUS_ZOMBIE
                except psutil.NoSuchProcess:
                    return True

            wait_for(child_stopped, timeout=5)
            worker = start("_worker")

            def recovered():
                job = client.get(route).json()
                return job if job["job_status"] == "failed" else None

            final = wait_for(recovered)
            ledger = final["budget"]
            assert ledger["known_started"] >= first["budget"]["known_started"]
            assert ledger["charged_unknown"] > 0 and ledger["reserved_active"] == 0
            assert ledger["known_started"] + ledger["charged_unknown"] <= ledger["max_objective_evaluations"]
            report = client.get(route + "/report").json()
            assert report["partial"] and report["report"]["best_x"] is None
            assert {item["kind"] for item in report["artifacts"]} == {"report_json", "report_html", "trials_csv"}
            assert client.post("/jobs", json=payload).json()["job_id"] == job_id

            # The restarted Worker can run a new job, and API cancellation owns its terminal state.
            second = client.post("/jobs", json=payload, headers={"Idempotency-Key": "TEST_FIXTURE_CANCEL"})
            assert second.status_code == 202
            route = f"/jobs/{second.json()['job_id']}"
            running = wait_for(active)
            cancel = client.post(route + "/cancel", json={"expected_job_version": running["version"]})
            assert cancel.status_code == 202
            acknowledged = cancel.json()
            assert acknowledged["job_status"] == "running" and acknowledged["cancel_requested"]

            def cancelled():
                job = client.get(route).json()
                return job if job["job_status"] == "cancelled" else None

            terminal = wait_for(cancelled)
            assert terminal["budget"]["known_started"] == acknowledged["budget"]["known_started"]
            assert terminal["budget"]["reserved_active"] == 0
            assert terminal["solution_status"] == "validation_incomplete"
            events = client.get(route + "/events").json()
            assert not any(row["payload"].get("phase") in {"verification", "verifying"} for row in events)
            report = client.get(route + "/report").json()
            assert report["partial"] and report["explanation_status"] == "not_requested"
            assert report["report"]["best_x"] is None
            assert report["report"]["verification"] is None and not report["report"]["llm_calls"]
            assert report["report"]["diagnostics"]["unverified_candidate"] is not None
            assert client.post(route + "/cancel", json={"expected_job_version": running["version"]}).status_code == 200
    finally:
        stop_path.touch()
        if worker is not None:
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                terminate_fixture(worker)
        terminate_fixture(api)
