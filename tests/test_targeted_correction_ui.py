"""T04/T05/T06: exercise the real correction form and its explicit requests."""
from copy import deepcopy
import json
import sys

import pytest
from streamlit.testing.v1 import AppTest

from p_mc_optimizer.domain.models import ProblemInput


QUESTION = "کران پایین و بالای متناهیِ هر یک از متغیرهای x1 و x2 چیست؟"
PATHS = [f"/variables/{index}/{bound}" for index in (0, 1) for bound in ("lower", "upper")]
LABELS = ["کران پایین x1", "کران بالای x1", "کران پایین x2", "کران بالای x2"]
EXHAUSTED_MESSAGE = (
    "صورت‌بندی مسئله در سهم مجاز تماس‌های مدل کامل نشد. "
    "برای ادامه، مدل کامل مسئله را در بخش تکمیل پیشرفته ثبت کنید."
)
ANSWER = "کران پایین x1 برابر -5 و کران بالای آن 5 است.\nکران پایین x2 برابر -5 و کران بالای آن 5 است."
PROBLEM = {
    "schema_version": "1.0",
    "variables": [{"name": name, "type": "real", "lower": -5, "upper": 5} for name in ("x1", "x2")],
    "objective": {"sense": "minimize", "source": {"kind": "expression", "expression": "(x1-2)**2+(x2+1)**2"}},
    "parameters": {}, "constraints": [], "deterministic": True,
}


def _correction_page():
    import streamlit as st

    from p_mc_optimizer.ui.executions import _correction_form

    def attempt(method, route, payload=None):
        st.session_state.calls.append({"method": method, "route": route, "payload": payload})
        # Keep this UI fixture in place after rerun so tests can inspect both request and form.
        return {"job_id": st.session_state.job["job_id"]}

    _correction_form(attempt, st.session_state.job["job_id"], st.session_state.job, st.session_state.draft)


def page(monkeypatch, *, question=None, exhausted=False, variables=None):
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    app = AppTest.from_function(_correction_page)
    app.session_state["calls"] = []
    app.session_state["job"] = {
        "job_id": "same-job", "version": 17, "job_status": "needs_input",
        "blocked_reason": "LLM_BUDGET_EXHAUSTED" if exhausted else None,
    }
    app.session_state["draft"] = {
        "draft_version": 3,
        "extracted_fields": {"variables": variables or [{"name": "x1"}, {"name": "x2"}]},
        "ambiguities": [question or {
            "question_id": "actual-question", "field_path": PATHS[0],
            "field_paths": PATHS, "question_fa": QUESTION, "reason": "missing",
        }],
    }
    app.run()
    assert not app.exception
    return app


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_grouped_bounds_keep_full_question_and_all_labels_and_submit_once(monkeypatch):
    """T06/T03: grouping changes presentation, not question identity or answer envelope."""
    app = page(monkeypatch)
    assert QUESTION in [item.value for item in app.text]
    assert [item.label for item in app.text_input] == ["پاسخ پرسش ۱"]
    assert [item.value for item in app.caption if item.value == "، ".join(LABELS)] == ["، ".join(LABELS)]
    assert app.text_input[0].key == "answer:same-job:3:actual-question"
    assert not app.session_state.calls
    app.text_input[0].set_value(ANSWER)
    button(app, "ثبت پاسخ و ادامه").click().run()
    assert not app.exception
    assert app.session_state.calls == [{
        "method": "POST", "route": "/jobs/same-job/answers", "payload": {
            "expected_job_version": 17, "expected_draft_version": 3, "mode": "textual_answers",
            "answers": [{"question_id": "actual-question", "answer": ANSWER}],
        },
    }]


@pytest.mark.parametrize("field_paths", [None, [], [PATHS[0], PATHS[0]]], ids=["legacy", "empty-list", "deduplicated"])
def test_single_resolvable_bound_can_use_short_label_with_legacy_fallback(monkeypatch, field_paths):
    question = {"question_id": "one", "field_path": PATHS[0], "question_fa": QUESTION, "reason": "missing"}
    if field_paths is not None:
        question["field_paths"] = field_paths
    app = page(monkeypatch, question=question)
    assert app.text[0].value == LABELS[0]
    assert app.text_input[0].label == LABELS[0]
    assert any(item.value == QUESTION for item in app.text)  # Detailed original question remains available.
    assert not app.session_state.calls


def test_grouped_paths_preserve_order_and_remove_duplicates_without_short_title(monkeypatch):
    paths = [PATHS[3], PATHS[0], PATHS[3], PATHS[1], PATHS[0], PATHS[2]]
    app = page(monkeypatch, question={
        "question_id": "group", "field_path": PATHS[0], "field_paths": paths,
        "question_fa": QUESTION, "reason": "missing",
    })
    assert app.text[0].value == QUESTION
    assert "، ".join([LABELS[3], LABELS[0], LABELS[1], LABELS[2]]) in [item.value for item in app.caption]


def test_nonempty_field_paths_override_legacy_first_path(monkeypatch):
    app = page(monkeypatch, question={
        "question_id": "preferred-path", "field_path": PATHS[0], "field_paths": [PATHS[3]],
        "question_fa": QUESTION, "reason": "missing",
    })
    assert app.text_input[0].label == LABELS[3]
    assert PATHS[3] in [item.value for item in app.text]
    assert PATHS[0] not in [item.value for item in app.text]


@pytest.mark.parametrize("paths", [["/parameters/a"], ["/variables/99/lower"], ["/variables/01/lower"], [None], [PATHS[0], "/parameters/a"]])
def test_unknown_or_mixed_paths_keep_full_question_in_main_content(monkeypatch, paths):
    app = page(monkeypatch, question={
        "question_id": "unknown", "field_path": PATHS[0], "field_paths": paths,
        "question_fa": QUESTION, "reason": "missing",
    })
    assert app.text[0].value == QUESTION
    assert app.text_input[0].label == "پاسخ پرسش ۱"


def test_unknown_legacy_null_path_keeps_full_question(monkeypatch):
    app = page(monkeypatch, question={
        "question_id": "legacy-null", "field_path": None, "question_fa": QUESTION, "reason": "missing",
    })
    assert app.text[0].value == QUESTION
    assert app.text_input[0].label == "پاسخ پرسش ۱"


def test_invalid_variable_name_does_not_create_a_misleading_short_bound_title(monkeypatch):
    app = page(monkeypatch, question={
        "question_id": "invalid-name", "field_path": PATHS[0],
        "question_fa": QUESTION, "reason": "missing",
    }, variables=[{"name": "<script>raw</script>"}])
    assert app.text[0].value == QUESTION
    assert app.text_input[0].label == "پاسخ پرسش ۱"


def test_exhausted_budget_hides_text_submission_without_inferring_why(monkeypatch):
    """T04: mere exhaustion cannot imply that an answer or a quote failure happened."""
    app = page(monkeypatch, exhausted=True)
    assert not app.text_input
    assert "ثبت پاسخ و ادامه" not in [item.label for item in app.button]
    assert app.info[0].value == EXHAUSTED_MESSAGE
    assert app.expander[0].label == "تکمیل پیشرفته با JSON"
    assert app.expander[0].proto.expanded
    assert app.text_area[0].value == ""
    assert any("کل مدل معتبر مسئله" in item.value and "دو یا چهار عدد" in item.value for item in app.caption)
    assert not app.session_state.calls
    app.run()  # Rerendering itself must not try the exhausted textual endpoint.
    assert not app.session_state.calls


def test_exhausted_budget_requires_explicit_entire_valid_problem_for_same_job(monkeypatch):
    """T05 UI: neither draft data nor rejected output is promoted automatically."""
    app = page(monkeypatch, exhausted=True)
    app.text_area[0].set_value('{"lower":-5,"upper":5}')
    button(app, "ثبت مدل کامل و ادامه").click().run()
    assert not app.exception
    assert app.error and not app.session_state.calls
    full_problem = deepcopy(PROBLEM)
    app.text_area[0].set_value(json.dumps(full_problem, ensure_ascii=False))
    assert not app.session_state.calls
    button(app, "ثبت مدل کامل و ادامه").click().run()
    assert not app.exception
    assert app.session_state.calls == [{
        "method": "POST", "route": "/jobs/same-job/answers", "payload": {
            "expected_job_version": 17, "expected_draft_version": 3, "mode": "structured_completion",
            "problem": ProblemInput.model_validate(full_problem).model_dump(mode="json"),
        },
    }]
