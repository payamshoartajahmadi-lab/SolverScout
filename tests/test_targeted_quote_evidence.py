"""Real failed formulation evidence stays exact through bound clarification."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from p_mc_optimizer.domain.models import ProblemInput
from p_mc_optimizer.llm.formulation import RoleOutputError, convert_formulation


_FIXTURES = Path(__file__).parent / "fixtures" / "targeted_formulation"
_ANSWER = (
    "کران پایین x1 برابر -5 و کران بالای آن 5 است.\n"
    "کران پایین x2 برابر -5 و کران بالای آن 5 است."
)


def fixture(name):
    return json.loads((_FIXTURES / (name + ".json")).read_text())


def sources(payload, answer=_ANSWER):
    answer_refs = {
        item["source_ref"] for item in payload["source_map"]
        if item["source_ref"].startswith("answer:")
    }
    return {
        "input:text": (_FIXTURES / "q08_input.txt").read_text(),
        **{ref: answer for ref in answer_refs},
    }


def with_intervals(intervals, sentences):
    """A separate fixture derivative; never modify the historical source fixture."""
    payload = fixture("q08_valid_exact_quote")
    for index, (lower, upper) in enumerate(intervals):
        payload["extracted_fields"]["variables"][index].update(lower=lower, upper=upper)
        for item in payload["source_map"]:
            if item["field_path"] in {f"/variables/{index}/lower", f"/variables/{index}/upper"}:
                item["quote"] = sentences[index]
    return payload, sources(payload, "\n".join(sentences))


def test_historical_rewritten_quote_is_rejected_with_location_without_source_text():
    """T01 / R01: correct numbers never excuse a fabricated quotation."""
    payload = fixture("q08_rejected")
    original = deepcopy(payload)
    item = payload["source_map"][13]
    assert item["field_path"] == "/variables/1/upper"
    with pytest.raises(RoleOutputError) as caught:
        convert_formulation(payload, sources(payload))
    error = caught.value.field_errors[0]
    assert "source_map[13].quote" in error
    assert item["field_path"] in error and item["source_ref"] in error
    assert "quote does not occur in source" in error
    assert item["quote"] not in error and _ANSWER not in error
    assert payload == original


def test_exact_sentence_derivative_preserves_numbers_and_accepts_complete_problem():
    """T02 / R02: only the invalid quotation differs from the recorded response."""
    original = fixture("q08_rejected")
    payload = fixture("q08_valid_exact_quote")
    expected = deepcopy(original)
    expected["source_map"][13]["quote"] = _ANSWER.splitlines()[1]
    assert payload == expected
    converted = convert_formulation(payload, sources(payload))
    problem = ProblemInput.model_validate(converted["problem"])
    assert [(variable.lower, variable.upper) for variable in problem.variables] == [(-5, 5)] * 2
    assert converted["missing_fields"] == converted["ambiguities"] == []
    assert converted["extracted_fields"] == original["extracted_fields"]
    evidence = next(item for item in converted["source_map"] if item["draft_field_path"] == "/variables/1/upper")
    assert evidence["quote"] == _ANSWER.splitlines()[1]
    for span in evidence["matches"]:
        assert _ANSWER[span["start"]:span["end"]] == evidence["quote"]


def test_two_distinct_pronoun_intervals_keep_each_numbers_and_full_sentence():
    """T07 / R02: bound tracing is not specific to the symmetric acceptance case."""
    sentences = (
        "کران پایین x1 برابر -7 و کران بالای آن 4 است.",
        "کران پایین x2 برابر -11 و کران بالای آن 8 است.",
    )
    payload, refs = with_intervals(((-7, 4), (-11, 8)), sentences)
    converted = convert_formulation(payload, refs)
    problem = ProblemInput.model_validate(converted["problem"])
    assert [(v.lower, v.upper) for v in problem.variables] == [(-7, 4), (-11, 8)]
    for index, sentence in enumerate(sentences):
        entries = [item for item in converted["source_map"]
                   if item["draft_field_path"] in {f"/variables/{index}/lower", f"/variables/{index}/upper"}]
        assert len(entries) == 2
        assert all(item["quote"] == sentence for item in entries)
    wrong = deepcopy(payload)
    next(item for item in wrong["source_map"] if item["field_path"] == "/variables/1/upper")["quote"] = sentences[0]
    with pytest.raises(RoleOutputError) as caught:
        convert_formulation(wrong, refs)
    assert caught.value.field_errors == ["/variables/1/upper: the number does not occur in the source quote"]


@pytest.mark.parametrize("sentences", [
    (
        "کران پایین x1 برابر −۷٫۵ و کران بالای آن ۴٫۲۵ است.",
        "کران پایین x2 برابر −۲۵ و کران بالای آن ۶۰ است.",
    ),
    (
        "کران پایین x1 برابر -7.5e0 و کران بالای آن 4.25e+0 است.",
        "کران پایین x2 برابر −۲٫۵e+۱ و کران بالای آن ۶e+۱ است.",
    ),
], ids=["persian-digits-unicode-minus", "scientific-and-persian-exponents"])
def test_numeric_normalization_preserves_exact_original_quotes(sentences):
    """T13 / R02: existing numeric normalization does not rewrite the evidence."""
    intervals = ((-7.5, 4.25), (-25, 60))
    payload, refs = with_intervals(intervals, sentences)
    payload["normalizations"] = [
        {"field_path": f"/variables/{index}/{side}", "source_text": sentences[index],
         "normalized_text": str(value), "reason_fa": "تبدیل شفاف نگارش عدد به مقدار عددی."}
        for index, interval in enumerate(intervals)
        for side, value in zip(("lower", "upper"), interval)
    ]
    converted = convert_formulation(payload, refs)
    problem = ProblemInput.model_validate(converted["problem"])
    assert [(v.lower, v.upper) for v in problem.variables] == list(intervals)
    assert converted["normalizations"] == payload["normalizations"]
    answer = "\n".join(sentences)
    for evidence in converted["source_map"]:
        if evidence["source_ref"].startswith("answer:"):
            assert evidence["quote"] in sentences
            assert all(answer[span["start"]:span["end"]] == evidence["quote"] for span in evidence["matches"])
    wrong = deepcopy(payload)
    wrong["normalizations"][0]["source_text"] = "کران پایین x1 برابر -7.5 است."
    with pytest.raises(RoleOutputError) as caught:
        convert_formulation(wrong, refs)
    assert "normalizations.source_text: quote does not occur in sources" in caught.value.field_errors


def test_invalid_q10_witness_is_rejected_before_returning_contradictory_bounds():
    """T14 / R10: even a genuine invalid interval cannot bypass evidence checks."""
    payload = fixture("q10_response")
    item = next(row for row in payload["source_map"] if row["field_path"] == "/variables/0/lower")
    item["quote"] = "کران پایین x1 برابر 50"
    with pytest.raises(RoleOutputError) as caught:
        convert_formulation(payload, {"input:text": (_FIXTURES / "q10_input.txt").read_text()})
    assert "/variables/0/lower" in caught.value.field_errors[0]
    assert "source_ref=input:text" in caught.value.field_errors[0]
    assert "quote does not occur in source" in caught.value.field_errors[0]
