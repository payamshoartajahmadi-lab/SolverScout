"""Persian Streamlit client. All state mutations go through the local API."""

from __future__ import annotations

import os

import httpx
import streamlit as st

from p_mc_optimizer.ui.catalog import catalog_page
from p_mc_optimizer.ui.connections import connections_page, model_settings
from p_mc_optimizer.ui.executions import executions_page
from p_mc_optimizer.ui.library import library_page
from p_mc_optimizer.ui.research import research
from p_mc_optimizer.ui.results import report_panel
from p_mc_optimizer.ui.theme import UI_CSS
from p_mc_optimizer.ui.web_research import source_settings
from p_mc_optimizer.ui.wizard import reset_wizard, wizard
from p_mc_optimizer.ui.workspace import LIBRARY, PROBLEM, RUNS, SETTINGS, navigate, navigation


class ApiFailure(Exception):
    pass


def call(method, route, data=None, *, idempotency_key=None):
    headers = {"X-PMC-Internal-Token": os.environ["PMC_INTERNAL_TOKEN"]}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    args = {"json": data} if data is not None else {}
    try:
        with httpx.Client(base_url=os.environ["PMC_API_URL"], trust_env=False, timeout=315) as client:
            reply = client.request(method, route, headers=headers, **args)
    except httpx.HTTPError:
        raise ApiFailure("ارتباط با سرویس محلی برقرار نشد.") from None
    if reply.is_error:
        try:
            body = reply.json()
            error = body.get("detail", body)
            raise ApiFailure(f"{error.get('error_code', '')} · {error.get('message_fa', 'درخواست پذیرفته نشد.')}")
        except (ValueError, AttributeError):
            raise ApiFailure("سرویس محلی پاسخ معتبر نداد.") from None
    return reply.content if route.startswith("/artifacts/") else reply.json()


def attempt(method, route, data=None, **kwargs):
    try:
        return call(method, route, data, **kwargs)
    except ApiFailure as error:
        st.error(str(error))
        return None


def open_problem(problem=None):
    reset_wizard(problem)
    navigate(PROBLEM)


def open_library():
    navigate(LIBRARY)


def open_connections(focus):
    st.session_state["connection-return"] = st.session_state.get("workspace-page", PROBLEM)
    st.session_state["connection-focus"] = focus
    navigate(SETTINGS)


def return_from_connections():
    navigate(st.session_state.get("connection-return", PROBLEM))


def open_execution(job_id):
    st.session_state.job_id = job_id
    st.session_state.pop("run-problem-summary", None)
    navigate(RUNS)
    st.rerun()


def show_report(job_id, job):
    report_panel(attempt, job_id, job, open_problem,
                 load_artifact=lambda artifact_id: call("GET", f"/artifacts/{artifact_id}"))


def library():
    st.title("منابع و الگوریتم‌ها")
    st.caption("منابع مجاز جست‌وجو و روش‌هایی را که این محیط می‌تواند اجرا کند بررسی کنید.")
    documents, sources, catalog, experiments = st.tabs(
        ["کتابخانهٔ منابع", "مجوز منابع", "الگوریتم‌ها", "آزمایش پژوهشی"],
        key="library-tab", on_change="rerun",
    )
    if documents.open:
        with documents:
            library_page(attempt)
    if sources.open:
        with sources:
            source_settings(attempt)
    if catalog.open:
        with catalog:
            catalog_page(attempt)
    if experiments.open:
        with experiments:
            research(attempt, open_execution=open_execution)


def connections():
    connections_page(
        attempt,
        on_back=return_from_connections if st.session_state.get("connection-return") else None,
        focus=st.session_state.get("connection-focus"),
    )


def main():
    st.set_page_config(page_title="SolverScout | بهینه‌ساز محلی", layout="wide", initial_sidebar_state="expanded")
    st.html(UI_CSS)
    pages = {
        PROBLEM: lambda: wizard(attempt, open_execution, open_connections, open_library),
        RUNS: lambda: executions_page(attempt, open_problem, model_settings, show_report),
        LIBRARY: library,
        SETTINGS: connections,
    }
    pages[navigation()]()


if __name__ == "__main__":
    main()
