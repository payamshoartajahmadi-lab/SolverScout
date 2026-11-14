"""Build a small SYNTHETIC_NOT_HISTORICAL repair-contract example.

This reads the existing F1 fixture and calls only pure reporting/context helpers.
It does not run pytest, a gateway, an HTTP request, an objective or a solver.
Run from the candidate worktree using its src directory on PYTHONPATH.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3, canonical_bytes
from p_mc_optimizer.reporting.narrative_validation import request_hash, section_hash, validate_draft
from p_mc_optimizer.workflow.narrative_v3 import build_writer_context


def data(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else deepcopy(value)


def synthetic_draft(context):
    """Construct free prose with required tokens, like the controlled test writer."""
    requirements = {s.section_id: s for s in context.evidence.sections}
    sections = []
    for section_id in context.section_scope:
        if section_id == "mathematical_analysis":
            continue
        requirement = requirements[section_id]
        sections.append({
            "section_id": section_id,
            "paragraphs": [{
                "paragraph_id": "p_" + section_id,
                "claim_kind": "recorded",
                "text_fa": "شرح مصنوعی برای بررسی قرارداد، بر پایهٔ شواهد fixture: "
                           + "؛ ".join("{{fact:" + ref + "}}" for ref in requirement.required_fact_refs),
                "fact_refs": list(requirement.required_fact_refs),
                "evidence_refs": list(requirement.allowed_evidence_refs),
            }],
        })
    return {"schema_version": "3.0", "context_hash": context.context_hash,
            "request_hash": context.request_hash, "sections": sections}


def validation_summary(result):
    return {"global_error": result.global_error,
            "valid_section_ids": [s.section_id for s in result.valid_sections],
            "issues": [data(i) for i in result.issues]}


def main():
    fixture_path = ROOT / "tests/fixtures/narrative_v3/F1.json"
    report = json.loads(fixture_path.read_text(encoding="utf-8"))
    evidence = build_evidence_v3(report, policy_version="1.1")
    initial = build_writer_context(evidence, prompt_bundle_sha256="a" * 64)
    invalid = synthetic_draft(initial)
    problem = invalid["sections"][0]
    assert problem["section_id"] == "problem"
    problem["paragraphs"][0]["paragraph_id"] = "problem-main"
    original = deepcopy(invalid)
    checked = validate_draft(initial, invalid)
    assert len(checked.invalid_sections) == 1 and not checked.global_error
    assert all(i.code == "SCHEMA_INVALID" for i in checked.issues)

    repair = build_writer_context(
        evidence, prompt_bundle_sha256="a" * 64, scope=["problem"],
        accepted_sections=checked.valid_sections, failed_sections=checked.invalid_sections,
        repair_issues=checked.issues,
    )
    accepted_hashes = [{"section_id": s.section_id, "sha256": section_hash(s)}
                       for s in repair.accepted_sections]
    corrected = synthetic_draft(repair)
    corrected["sections"][0]["paragraphs"][0]["paragraph_id"] = "problem_main"
    fixed = validate_draft(repair, corrected)
    assert not fixed.global_error and not fixed.issues
    assert request_hash(repair) == repair.request_hash

    missing_sense = deepcopy(corrected)
    missing_sense["sections"][0]["paragraphs"][0]["text_fa"] = (
        missing_sense["sections"][0]["paragraphs"][0]["text_fa"]
        .replace("{{fact:problem.sense}}", "کمینه‌سازی")
    )
    missing = validate_draft(repair, missing_sense)
    codes = {i.code for i in missing.issues}
    assert codes == {"INVALID_BINDING", "MISSING_REQUIRED_FACT"}
    assert invalid == original
    assert accepted_hashes == [{"section_id": s.section_id, "sha256": section_hash(s)}
                               for s in repair.accepted_sections]

    source_paths = [
        "src/p_mc_optimizer/llm/narrative_models.py",
        "src/p_mc_optimizer/llm/prompts/explainer.v3.1.txt",
        "src/p_mc_optimizer/reporting/narrative_validation.py",
        "src/p_mc_optimizer/workflow/narrative_v3.py",
    ]
    output = {
        "evidence_kind": "SYNTHETIC_NOT_HISTORICAL",
        "scope": "Pure offline contract illustration; no historical C-L1 raw response or live model acceptance claim.",
        "fixture": {"path": fixture_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(fixture_path.read_bytes()).hexdigest()},
        "source_files": [{"path": p, "sha256": sha256((ROOT / p).read_bytes()).hexdigest()}
                         for p in source_paths],
        "synthetic_prompt_bundle_sha256": "a" * 64,
        "generation_policy_version": "1.1",
        "invalid_problem_section": problem,
        "initial_validation": validation_summary(checked),
        "repair": {"mode": repair.mode, "section_scope": list(repair.section_scope),
                   "context_hash": repair.context_hash, "request_hash": repair.request_hash,
                   "request_hash_matches_context": request_hash(repair) == repair.request_hash,
                   "failed_sections": [data(s) for s in repair.failed_sections],
                   "repair_issues": [data(i) for i in repair.repair_issues],
                   "accepted_section_hashes": accepted_hashes,
                   "accepted_sections_semantically_reviewed_in_this_generator": False},
        "corrected_problem_section": corrected["sections"][0],
        "corrected_validation": {"status": "PASS", **validation_summary(fixed)},
        "valid_id_missing_sense_control": {
            "expected_rejection_observed": True,
            "input_problem_section": missing_sense["sections"][0],
            **validation_summary(missing),
        },
        "raw_input_unchanged": invalid == original,
        "accepted_section_hashes_unchanged": True,
        "new_model_calls": 0,
        "new_objective_evaluations": 0,
        "new_solver_calls": 0,
        "pytest_execution": "NOT_RUN_BY_THIS_GENERATOR",
        "live_acceptance": "NOT_RUN",
    }
    target = Path(__file__).with_name("synthetic-repair-example.json")
    target.write_text(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps({"path": target.relative_to(ROOT).as_posix(),
                      "evidence_kind": output["evidence_kind"],
                      "bytes": target.stat().st_size,
                      "sha256": sha256(target.read_bytes()).hexdigest(),
                      "initial_issue_codes": [i.code for i in checked.issues],
                      "corrected_validation": "PASS",
                      "missing_sense_issue_codes": sorted(codes),
                      "accepted_sections": len(accepted_hashes),
                      "new_model_calls": 0, "new_objective_evaluations": 0, "new_solver_calls": 0}))


if __name__ == "__main__":
    main()
