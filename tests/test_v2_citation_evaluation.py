import copy
import importlib.util
from pathlib import Path

import pytest


script_path = Path(__file__).resolve().parents[1] / "scripts/evaluate_citations_v2.py"
spec = importlib.util.spec_from_file_location("citation_evaluation_v2", script_path)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_citation_control_resolves_all_sources():
    result = evaluation.evaluate(evaluation.load(evaluation.CORPUS), evaluation.load(evaluation.CONTROL))
    assert result["case_count"] == 24
    assert result["counts_by_verdict"] == {"supported": 8, "contradictory": 8, "irrelevant": 8}
    assert result["verdict_accuracy"] == 1.0
    assert result["passed"]


def test_high_label_accuracy_cannot_hide_wrong_attribution():
    corpus = evaluation.load(evaluation.CORPUS)
    predictions = evaluation.load(evaluation.CONTROL)
    predictions[0]["citations"] = copy.deepcopy(predictions[3]["citations"])
    predictions[3]["citations"][0]["source_url"] = "https://made-up.example.invalid/forged"
    predictions[3]["implementation_id"] = "fixture:compass:OriginalV1"
    predictions[18]["trial_id"] = "trial-beacon-1"

    result = evaluation.evaluate(corpus, predictions)
    assert result["verdict_accuracy"] == 1.0
    assert result["invalid_provenance"] == ["citation-01", "citation-04"]
    assert result["wrong_variant_attribution"] == ["citation-04"]
    assert result["wrong_execution_attribution"] == ["citation-19"]
    assert not result["passed"]


def test_missing_predictions_lower_accuracy_and_fail_coverage():
    predictions = evaluation.load(evaluation.CONTROL)[1:]
    result = evaluation.evaluate(evaluation.load(evaluation.CORPUS), predictions)
    assert result["verdict_accuracy"] == 23 / 24
    assert result["missing_predictions"] == ["citation-01"]
    assert not result["passed"]


@pytest.mark.parametrize(("tamper", "error"), [("text", "Invalid document hash"), ("offset", "Invalid passage provenance")])
def test_corpus_provenance_rejects_corruption(tamper, error):
    corpus = evaluation.load(evaluation.CORPUS)
    if tamper == "text":
        corpus["documents"][0]["text"] += " edited"
    else:
        corpus["documents"][0]["passages"][0]["start"] += 1
    with pytest.raises(ValueError, match=error):
        evaluation.evaluate(corpus, evaluation.load(evaluation.CONTROL))
