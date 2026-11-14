"""TEST FIXTURE research routing and Streamlit submission contracts."""

from pathlib import Path
from uuid import uuid4

import sys

import pytest
from streamlit.testing.v1 import AppTest

from p_mc_optimizer.benchmarks import Manifest, load_manifest, plan_jobs, run_benchmark
from p_mc_optimizer.reporting import json_bytes


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_research_manifest_snapshots_model_only_for_llm_jobs(tmp_path):
    baseline, _ = load_manifest(EXAMPLES / "benchmark_smoke.json")
    profile = {"profile_id": str(uuid4()), "revision": 2}
    manifest = Manifest.model_validate(baseline.model_dump(mode="json") | {
        "provider_profile_revision": profile,
        "policy_ids": ["pilot_only", "llm_shortlist_then_pilot"],
        "reference_file": "INTENTIONALLY_MISSING_REFERENCE.json",
        "instances": [baseline.instances[0].model_dump(mode="json")],
        "master_seeds": [1405],
    })
    planned = [job.model_dump(mode="json") for _, job in plan_jobs(manifest, EXAMPLES)]
    assert len(planned) == 2
    assert [job["llm_profile"] for job in planned] == [None, profile]
    assert planned[0]["input"] == planned[1]["input"]
    for job in planned:
        assert job["input"]["kind"] == "structured_problem"
        assert not job["policy"]["explain_via_llm"]
        assert b"f_star" not in json_bytes(job) and b"x_star" not in json_bytes(job)

    path = tmp_path / "manifest.json"
    path.write_bytes(json_bytes(manifest.model_dump(mode="json")))
    with pytest.raises(ValueError, match="CLI"):
        run_benchmark(None, path, lambda *_: pytest.fail("offline CLI submitted a model job"))


def test_ui_requires_explicit_click_and_reuses_comparison(tmp_path, monkeypatch):
    # Streamlit replaces __main__ while executing the test script.
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    app = AppTest.from_string('''
from uuid import uuid4
import streamlit as st
from p_mc_optimizer.ui.research import research

def attempt(method, route, data=None, **kwargs):
    if route == "/settings/llm/profile":
        return {"profile": {"test_status": "passed", "profile_id": "b1ad514a-90f5-4b98-8282-3946b3da4d69",
                            "revision": 3, "base_url": "https://fixture.invalid/v1", "model": "TEST FIXTURE"},
                "credential_status": {"available": True}}
    if method == "POST":
        st.session_state["submissions"] = [*st.session_state.get("submissions", []), (data, kwargs)]
        return {"job_id": str(uuid4())}
    return {"job_status": "queued", "solution_status": "pending"}

research(attempt)
''').run()
    assert not app.exception
    assert "submissions" not in app.session_state
    app.button[0].click().run()
    assert not app.exception
    submitted = app.session_state["submissions"]
    assert len(submitted) == 2
    assert [row[0]["policy"]["selection_policy"] for row in submitted] == ["fixed_solver", "llm_only"]
    assert all(row[0]["policy"]["explain_via_llm"] is False for row in submitted)
    assert len({row[1]["idempotency_key"] for row in submitted}) == 2
    app.button[0].click().run()
    assert not app.exception
    assert len(app.session_state["submissions"]) == 2
