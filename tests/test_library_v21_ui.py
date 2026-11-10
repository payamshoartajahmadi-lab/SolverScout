"""Exercise the Streamlit controls against local APIs and fixture sources."""

import sys
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from p_mc_optimizer.api import create_app
from p_mc_optimizer.reporting import html_bytes
from p_mc_optimizer.research.transport import Response

TOKEN = "library-ui-test"
SOURCE = {
    "source_id": "fixture", "label": "راهنمای روش‌ها", "hostname": "methods.example.test",
    "path_prefixes": ["/manual"], "source_kind": "official_docs", "enabled": True,
}
DOC_URL = "https://methods.example.test/manual/de"


class FixtureTransport:
    def __init__(self):
        self.requests = []

    def get(self, url, timeout, size, conditional):
        self.requests.append(url)
        if url == "https://methods.example.test/robots.txt":
            return Response(404, httpx.Headers(), b"")
        assert url == DOC_URL
        return Response(200, httpx.Headers({"Content-Type": "text/plain"}),
                        b"Differential Evolution for bounded continuous optimization.")


def _library_page():
    import streamlit as st

    from p_mc_optimizer.ui.library import library_page

    def attempt(method, route, payload=None, *, idempotency_key=None):
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        response = st.session_state.client.request(method, route, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

    library_page(attempt)


def _find(items, label):
    return next(item for item in items if item.label == label)


def test_library_ui_configures_refresh_and_searches_exact_stored_document(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    transport = FixtureTransport()
    app = create_app(tmp_path, TOKEN, library_transport=transport)
    with TestClient(app, headers={"X-PMC-Internal-Token": TOKEN}) as client:
        sources = client.get("/settings/research-sources").raise_for_status().json()
        client.put(
            "/settings/research-sources", json={"expected_revision": sources["revision"], "sources": [SOURCE]},
            headers={"Idempotency-Key": str(uuid4())},
        ).raise_for_status()
        page = AppTest.from_function(_library_page)
        page.session_state["client"] = client
        page.run()
        assert not page.exception
        _find(page.text_area, "نشانی آغازین سند، هر نشانی در یک سطر").set_value(DOC_URL)
        _find(page.button, "ثبت تنظیمات دریافت منبع").click().run()
        assert not page.exception
        _find(page.multiselect, "منابع برای به‌روزرسانی").select("fixture").run()
        _find(page.button, "شروع به‌روزرسانی").click().run()
        assert not page.exception
        run_id = page.session_state["library-sync-selection"]
        deadline = monotonic() + 5
        while monotonic() < deadline:
            run = client.get(f"/library/sync-runs/{run_id}").raise_for_status().json()
            if run["status"] not in {"queued", "running"}:
                break
            sleep(0.05)
        assert run["status"] == "completed"
        assert transport.requests == ["https://methods.example.test/robots.txt", DOC_URL]
        _find(page.text_input, "جست‌وجو در متن اسناد").set_value("Differential Evolution").run()
        assert not page.exception
        document = client.get("/library/documents").raise_for_status().json()["documents"][0]
        _find(page.selectbox, "سند").select(document["document_id"]).run()
        assert not page.exception
        assert any("Differential Evolution for bounded" in item.value for item in page.text)
        assert any("هش متن" in item.value for item in page.text)
        assert len(transport.requests) == 2


def _wizard_page():
    import streamlit as st

    from p_mc_optimizer.ui.wizard import DRAFT_KEY, wizard

    if DRAFT_KEY not in st.session_state:
        st.session_state[DRAFT_KEY] = {
            "step": 2, "mode": "text", "values": {},
            "problem": {"kind": "natural_language", "text": "تابع درجه‌دو را کمینه کن"},
        }

    def attempt(method, route, payload=None, *, idempotency_key=None):
        st.session_state.calls.append((method, route))
        if route == "/jobs":
            st.session_state.payload = payload
            return {"job_id": "fixture"}
        if route == "/settings/retrieval":
            return {"retrieval_mode": "local_only", "web_search_enabled": False}
        if route == "/settings/llm/profile":
            return {"profile": {
                "profile_id": st.session_state.profile_id, "revision": 1, "test_status": "passed",
                "base_url": "https://fixture.example.test", "model": "fixture-model",
            }, "credential_status": {"available": True, "credential_generation": 1}}
        if route == "/library/sources":
            return {"source_revision": 1, "sources": [{"rule": st.session_state.source, "usable_documents": 1}]}
        if route.startswith("/catalog/implementations"):
            return {"total": 3, "catalog_hash": "a" * 64}
        raise AssertionError(f"Unexpected API call {method} {route}")

    wizard(attempt, lambda job_id: st.session_state.update({"opened": job_id}), lambda *_: None, lambda: None)


def test_wizard_submits_default_local_library_without_contacting_search_provider(monkeypatch):
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    page = AppTest.from_function(_wizard_page)
    page.session_state["profile_id"] = str(uuid4())
    page.session_state["source"] = SOURCE
    page.session_state["calls"] = []
    page.run()
    assert not page.exception
    assert _find(page.radio, "مسیر پژوهش").value == "local_only"
    _find(page.button, "ادامه: مرور درخواست").click().run()
    assert not page.exception
    _find(page.button, "شروع پژوهش و حل").click().run()
    assert not page.exception
    payload = page.session_state["payload"]
    assert payload["schema_version"] == "2.1"
    assert payload["policy"]["selection_policy"] == "evidence_research_then_pilot"
    assert payload["policy"]["retrieval_mode"] == "local_only"
    assert payload["search_provider_ref"] is None
    assert not any("search" in route for _, route in page.session_state["calls"])


def _result_page():
    import streamlit as st

    from p_mc_optimizer.ui.results import report_panel

    report = st.session_state.recorded_report

    def attempt(method, route):
        assert method == "GET" and route == "/jobs/recorded/report"
        return {"report": report, "artifacts": []}

    report_panel(attempt, "recorded", report, lambda: None, load_artifact=lambda _: None)


@pytest.mark.parametrize(
    "status,caption,heading",
    [
        ("generated", "توضیح مدل زبانی", "توضیح مدل"),
        ("schema_fallback", "توضیح خودکار برنامه", "توضیح قالبی برنامه"),
    ],
)
def test_recorded_explanation_status_is_independent_of_verified_answer(status, caption, heading, monkeypatch):
    """VIEW-01/02/03. Existing saved v1 reports render without an Explainer context."""
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    report = {
        "schema_version": "1.0",
        "job_id": "recorded",
        "job_status": "completed",
        "solution_status": "valid_best_found",
        "selected_algorithm_id": "de.original",
        "best_x": [2.0, -1.0],
        "objective_raw": 0.0,
        "explanation_status": status,
        "explanation": {
            "summary_fa": "بهترین مشاهدهٔ معتبر ثبت شد.",
            "selection_explanation_fa": "روش ثبت‌شده برای اجرای اصلی به کار رفت.",
            "limitations_fa": ["بهینگی سراسری اثبات نشده است."],
            "evidence_refs": ["report:solution", "report:decision"],
        },
    }
    page = AppTest.from_function(_result_page)
    page.session_state["recorded_report"] = report
    page.run()
    assert not page.exception
    assert page.title[0].value == "جواب معتبر پیدا شد"
    assert page.metric[0].value == "0"
    assert any(item.value == caption for item in page.caption)
    assert any("تأیید شد" in item.value for item in page.success)
    exported = html_bytes(report).decode()
    assert f"<h2>{heading}</h2>" in exported
    assert "<code>valid_best_found</code>" in exported
    assert "روش ثبت‌شده برای اجرای اصلی به کار رفت." in exported
    assert "decision_summary" not in exported
