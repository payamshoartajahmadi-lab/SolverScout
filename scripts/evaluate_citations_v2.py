#!/usr/bin/env python3
"""Score citation and attribution predictions against the frozen v2 corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "examples/citations/synthetic_v2.json"
CONTROL = ROOT / "examples/citations/control_predictions_v2.json"
VERDICTS = {"supported", "contradictory", "irrelevant"}


def _unique_pairs(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError(f"Duplicate JSON key: {key}")
        values[key] = value
    return values


def _nonfinite(token):
    raise ValueError(f"Nonfinite JSON value: {token}")


def load(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_pairs, parse_constant=_nonfinite,
    )


def _unique(rows, field):
    indexed = {}
    for row in rows:
        key = row[field]
        if not isinstance(key, str) or not key or key in indexed:
            raise ValueError(f"Invalid or duplicate {field}: {key!r}")
        indexed[key] = row
    return indexed


def _index_corpus(corpus):
    if corpus["schema_version"] != "2.0" or corpus["corpus_kind"] != "synthetic":
        raise ValueError("Unsupported corpus")
    evidence = {}
    for document in _unique(corpus["documents"], "document_id").values():
        text = document["text"]
        if hashlib.sha256(text.encode()).hexdigest() != document["text_sha256"]:
            raise ValueError(f"Invalid document hash: {document['document_id']}")
        for key, passage in _unique(document["passages"], "evidence_id").items():
            start, end = passage["start"], passage["end"]
            if (
                key in evidence or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(text) or text[start:end] != passage["text"]
            ):
                raise ValueError(f"Invalid passage provenance: {key}")
            evidence[key] = (document["job_id"], document["source_url"])
    trials = _unique(corpus["trials"], "trial_id")
    cases = _unique(corpus["cases"], "case_id")
    if len(cases) < 20 or set(row["verdict"] for row in cases.values()) != VERDICTS:
        raise ValueError("The frozen corpus requires 20+ cases across all verdicts")
    for case_id, row in cases.items():
        refs = row["evidence_refs"]
        if not refs or any(key not in evidence or evidence[key][0] != row["job_id"] for key in refs):
            raise ValueError(f"Invalid oracle evidence: {case_id}")
        if row["trial_id"]:
            trial = trials.get(row["trial_id"])
            if (
                trial is None or trial["job_id"] != row["job_id"]
                or trial["implementation_id"] != row["implementation_id"]
            ):
                raise ValueError(f"Invalid oracle trial: {case_id}")
    return cases, evidence, trials


def evaluate(corpus, predictions):
    cases, evidence, trials = _index_corpus(corpus)
    rows = _unique(predictions, "case_id")
    extras = sorted(rows.keys() - cases.keys())
    missing = sorted(cases.keys() - rows.keys())
    verdict_errors, provenance_errors, variant_errors, execution_errors = [], [], [], []
    for case_id, prediction in rows.items():
        if case_id not in cases:
            continue
        if set(prediction) != {"case_id", "verdict", "citations", "implementation_id", "trial_id"}:
            raise ValueError(f"Unexpected prediction fields: {case_id}")
        oracle = cases[case_id]
        verdict = prediction["verdict"]
        if not isinstance(verdict, str) or verdict not in VERDICTS or verdict != oracle["verdict"]:
            verdict_errors.append(case_id)
        refs = []
        cites = prediction["citations"]
        if not isinstance(cites, list):
            raise ValueError(f"Citations must be a list: {case_id}")
        for cite in cites:
            if not isinstance(cite, dict) or set(cite) != {"evidence_id", "source_url"}:
                raise ValueError(f"Invalid citation fields: {case_id}")
            key = cite["evidence_id"]
            if not isinstance(key, str):
                raise ValueError(f"Invalid evidence ID: {case_id}")
            refs.append(key)
            if evidence.get(key) != (oracle["job_id"], cite["source_url"]):
                provenance_errors.append(case_id)
        if Counter(refs) != Counter(oracle["evidence_refs"]):
            provenance_errors.append(case_id)
        if prediction["implementation_id"] != oracle["implementation_id"]:
            variant_errors.append(case_id)
        trial_id = prediction["trial_id"]
        if trial_id is not None and not isinstance(trial_id, str):
            raise ValueError(f"Invalid trial ID: {case_id}")
        trial = trials.get(trial_id) if trial_id is not None else None
        if (
            trial_id != oracle["trial_id"] or trial_id is not None and (
                trial is None or trial["job_id"] != oracle["job_id"]
                or trial["implementation_id"] != prediction["implementation_id"]
            )
        ):
            execution_errors.append(case_id)
    count = len(cases)
    accuracy = (count - len(missing) - len(verdict_errors)) / count
    return {
        "case_count": count,
        "counts_by_verdict": dict(sorted(Counter(row["verdict"] for row in cases.values()).items())),
        "verdict_accuracy": accuracy,
        "minimum_accuracy": 0.9,
        "missing_predictions": missing,
        "unexpected_predictions": extras,
        "misclassified": sorted(verdict_errors),
        "invalid_provenance": sorted(set(provenance_errors)),
        "wrong_variant_attribution": sorted(variant_errors),
        "wrong_execution_attribution": sorted(execution_errors),
        "passed": accuracy >= 0.9 and not (
            missing or extras or provenance_errors or variant_errors or execution_errors
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--control", action="store_true", help="Score known fixture answers to verify the harness")
    mode.add_argument("--predictions", type=Path, help="Score independently generated predictions")
    args = parser.parse_args(argv)
    path = CONTROL if args.control else args.predictions
    result = evaluate(load(CORPUS), load(path))
    result["evaluation_mode"] = "fixture_control" if args.control else "submitted_predictions"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
