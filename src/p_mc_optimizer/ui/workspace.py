"""Navigation shared by the desktop sidebar and the narrow workspace menu."""

from __future__ import annotations

import streamlit as st

PROBLEM = "problem"
RUNS = "runs"
LIBRARY = "library"
SETTINGS = "settings"

_DESTINATIONS = (
    (PROBLEM, "حل مسئله", ":material/target:"),
    (RUNS, "اجراهای من", ":material/layers:"),
    (LIBRARY, "منابع و الگوریتم‌ها", ":material/menu_book:"),
    (SETTINGS, "تنظیمات", ":material/settings:"),
)


def navigate(page: str) -> None:
    st.session_state["workspace-page"] = page
    if page != SETTINGS:
        st.session_state.pop("connection-return", None)
        st.session_state.pop("connection-focus", None)


def _select_destination(page: str) -> None:
    st.session_state["workspace-menu"] = False
    if page == RUNS:
        st.session_state.pop("job_id", None)
    navigate(page)


def _links(surface: str, active: str) -> None:
    for page, label, icon in _DESTINATIONS:
        with st.container(key=f"{surface}-{'selected' if active == page else 'link'}-{page}"):
            st.button(
                label, key=f"{surface}-{page}", icon=icon, type="tertiary", width="stretch",
                on_click=_select_destination, args=(page,),
            )


def navigation() -> str:
    active = st.session_state.get("workspace-page", PROBLEM)
    with st.sidebar:
        with st.container(key="workbench-brand"):
            st.markdown("**SolverScout**")
        _links("desktop-nav", active)
    with st.container(key="mobile-navigation", horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        with st.popover("منو", icon=":material/menu:", key="workspace-menu", on_change="rerun") as menu:
            if menu.open:
                _links("mobile-nav", active)
        st.markdown("**SolverScout**")
    return active
