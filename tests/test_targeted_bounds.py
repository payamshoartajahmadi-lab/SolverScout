"""Targeted Q10 regressions through the real gateway, converter and workflow."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from p_mc_optimizer.llm.models import FormulatorResponse
from p_mc_optimizer.workflow.graph import _inverted_bounds_message
from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, checked
from test_llm_workflow import configure, fixture_transport, local_api, submit, work


FIXTURES = Path(__file__).parent / "fixtures" / "targeted_formulation"
MESSAGE = "کران پایین {name} از کران بالای آن بزرگ‌تر است. کران‌های مسئله را اصلاح کنید."
UNNAMED_MESSAGE = MESSAGE.format(name="یکی از متغیرها")


def historical_response():
    return json.loads((FIXTURES / "q10_response.json").read_text())


def variant(bounds, *, names=("x1", "x2"), kind=None):
    """Derive witnessed inputs for other bounds; never alter the historical fixture."""
    output = historical_response()
    text = (
        "تابع x1**2 + x2**2 را کمینه کن. هر دو حقیقی‌اند. تابع قطعی است. "
        "پارامتر جداگانه‌ای وجود ندارد. هیچ قید عمومی دیگری وجود ندارد.\n"
    )
    sentences = []
    for index, ((lower, upper), name) in enumerate(zip(bounds, names)):
        output["extracted_fields"]["variables"][index].update(name=name, lower=lower, upper=upper)
        sentence = f"برای {name} کران پایین برابر {lower} و کران بالا برابر {upper} است."
        sentences.append(sentence)
        text += sentence + "\n"
    source_map = []
    for row in output["source_map"]:
        row = deepcopy(row)
        pieces = row["field_path"].split("/")
        if pieces[1] == "variables":
            index = int(pieces[2])
            if pieces[3] in {"lower", "upper"}:
                value = bounds[index][0 if pieces[3] == "lower" else 1]
                if value is None:
                    continue
                row["quote"] = sentences[index]
            elif pieces[3] == "name":
                row["quote"] = names[index]
        source_map.append(row)
    output["source_map"] = source_map
    output["unsupported_features"] = [] if kind is None else [{
        "kind": kind, "description_fa": "این مورد برای بررسی مسیر خطا ثبت شده است.",
        "evidence_refs": ["input:text"],
    }]
    return text, output


def run_response(tmp_path, text, output, *, repeated_invalid=False):
    """Keep real HTTP loopback/gateway DTO conversion; only the provider is mocked."""
    called = []
    responses = [{"ok": True}, deepcopy(output)]
    if repeated_invalid:
        responses.append(deepcopy(output))
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, text)
        work(repo, address, token, job_id, legacy_explanation=False)
        state = checked(client.get(f"/jobs/{job_id}"))
        problem = checked(client.get(f"/jobs/{job_id}/problem"))
        assert state["budget"]["known_started"] == 0
        assert state["budget"]["reserved_active"] == state["budget"]["charged_unknown"] == 0
        assert problem["revision"] is None
        assert repo.list_executions(job_id) == repo.list_trials(job_id) == []
        with repo.connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM stage_executions WHERE job_id=?", (job_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM trial_attempts WHERE trial_id IN "
                "(SELECT trial_id FROM trials WHERE job_id=?)", (job_id,),
            ).fetchone()[0] == 0
        report, html = None, None
        if state["job_status"] == "failed":
            bundle = checked(client.get(f"/jobs/{job_id}/report"))
            report = bundle["report"]
            artifact = next(row for row in bundle["artifacts"] if row["kind"] == "report_html")
            html = client.get(f"/artifacts/{artifact['artifact_id']}").raise_for_status().text
            assert report["termination_reason"] == "validation_error"
            assert report["solution_status"] == "no_valid_solution"
            assert report["error"] == state["error"]
            assert report["error"]["message_fa"] in html
        assert "explainer_v2" not in called and "explainer_v1" not in called
        assert called == ["connection_test_v1"] + ["formulator_v1"] * (2 if repeated_invalid else 1)
        assert not responses
        return state, problem, report, html


@pytest.mark.parametrize("kind", ["inconsistent_variable_bounds", "inconsistent_bounds", "arbitrary_model_label", None])
def test_recorded_q10_real_provider_path_is_independent_of_kind(tmp_path, kind):
    """T09/T10: state, JSON and HTML agree; every numerical ledger stays empty."""
    output = historical_response()
    if kind is None:
        output["unsupported_features"] = []
    else:
        output["unsupported_features"][0]["kind"] = kind
    text = (FIXTURES / "q10_input.txt").read_text()
    state, problem, report, _ = run_response(tmp_path, text, output)
    assert state["job_status"] == "failed"
    assert state["error"] == {
        "error_code": "UNSUPPORTED_PROBLEM", "message_fa": MESSAGE.format(name="x1"),
    }
    assert report["error"] == state["error"]
    assert problem["draft"]["unsupported_features"] == output["unsupported_features"]
    assert problem["draft"]["extracted_fields"]["variables"] == output["extracted_fields"]["variables"]


@pytest.mark.parametrize("bounds,expected", [
    (((-5, 5), (3, -3)), "x2"),
    (((5, -5), (3, -3)), "x1"),
    (((None, 5), (3, -3)), "x2"),
])
def test_first_confirmed_inverted_variable_is_named(tmp_path, bounds, expected):
    """T11: choose the first complete contradiction in declared variable order."""
    text, output = variant(bounds)
    state, problem, _, _ = run_response(tmp_path, text, output)
    assert state["error"]["message_fa"] == MESSAGE.format(name=expected)
    assert [(v["lower"], v["upper"]) for v in problem["draft"]["extracted_fields"]["variables"]] == list(bounds)


def test_invalid_variable_name_uses_server_only_unnamed_message(tmp_path):
    """T14: the DTO permits an extracted raw name; the server never interpolates it."""
    name = "<script>unsafe_name</script>"
    text, output = variant(((5, -5), (-3, 3)), names=(name, "x2"))
    state, _, report, html = run_response(tmp_path, text, output)
    assert state["error"]["message_fa"] == report["error"]["message_fa"] == UNNAMED_MESSAGE
    assert name not in state["error"]["message_fa"] and name not in html


@pytest.mark.parametrize("fault", ["quote", "unsupported_evidence"])
def test_invalid_evidence_is_rejected_before_inverted_bound_message(tmp_path, fault):
    """T14/R10: malicious or ungrounded evidence never reaches numerical diagnosis."""
    output = historical_response()
    if fault == "quote":
        next(row for row in output["source_map"] if row["field_path"] == "/variables/0/upper")["quote"] = "عدد ساختگی خارج از منبع"
    else:
        output["unsupported_features"][0]["evidence_refs"] = ["answer:untrusted-identifier"]
    state, problem, report, _ = run_response(
        tmp_path, (FIXTURES / "q10_input.txt").read_text(), output, repeated_invalid=True,
    )
    assert state["error"]["error_code"] == report["error"]["error_code"] == "LLM_SCHEMA_INVALID"
    assert "بزرگ‌تر" not in state["error"]["message_fa"]
    assert problem["draft"] is None


@pytest.mark.parametrize("bounds,kind,expected_status", [
    (((None, 5), (-3, 3)), None, "needs_input"),
    (((-5, -1), (-3, -1)), "inconsistent_bounds", "failed"),
    (((5, 5), (-3, 3)), None, "failed"),
])
def test_missing_valid_negative_and_equal_bounds_keep_existing_paths(tmp_path, bounds, kind, expected_status):
    """T12: neither an absent value nor an error label creates a numeric contradiction."""
    text, output = variant(bounds, kind=kind)
    state, problem, _, _ = run_response(tmp_path, text, output)
    assert state["job_status"] == expected_status
    assert "بزرگ‌تر" not in (state.get("error") or {}).get("message_fa", "")
    if kind is not None:
        assert state["error"]["message_fa"] == "مسئله خارج از دامنهٔ نسخهٔ اول است."
    assert problem["draft"]["extracted_fields"]["variables"][0]["lower"] == bounds[0][0]


@pytest.mark.parametrize("value", [True, False, float("inf"), float("-inf"), float("nan"), "5"])
def test_non_finite_and_non_numeric_bounds_are_not_confirmed(value):
    """T12/T14: FiniteReal remains the input contract, with no string/boolean coercion."""
    output = historical_response()
    output["extracted_fields"]["variables"][0]["lower"] = value
    with pytest.raises(ValidationError):
        FormulatorResponse.model_validate(output)
    assert _inverted_bounds_message({"variables": [{"name": "x1", "lower": value, "upper": -5.0}]}) is None
