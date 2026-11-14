import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from p_mc_optimizer.domain.models import DomainError
from p_mc_optimizer.research.contracts import (
    AdvisorPlanV2,
    EvidencePassage,
    ExecutionPolicyInputV2,
    ExecutionPolicyV2,
    ResearchLimits,
    ResearchQueryPlan,
    SourceDocument,
    SourceRule,
    SourceSnapshot,
)
from p_mc_optimizer.research.source_policy import SourcePolicy, SourcePolicyError, require_public_ip


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def document_examples():
    spec = (ROOT / "docs/P_MC_Web_Research_Optimization_Development_Spec_v2.0.md").read_text()
    return tuple(part.split("\n```", 1)[0] for part in spec.split("```json\n")[1:])


@pytest.fixture
def policy(document_examples):
    rule = SourceRule.from_json(document_examples[0])
    return SourcePolicy(SourceSnapshot(revision=1, sources=(rule,)))


def test_spec_examples_and_runtime_schemas(document_examples, policy):
    source_text, plan_text = document_examples
    assert SourceRule.from_json(source_text).source_id == "mealpy_repository"
    plan = ResearchQueryPlan.from_json(plan_text).normalized(policy.snapshot)
    assert plan.queries[0].query_id == "q1"
    for name, model in (("source_rule", SourceRule), ("research_query_plan", ResearchQueryPlan)):
        assert json.loads((ROOT / f"schemas/{name}.v2.json").read_text()) == model.model_json_schema()


def test_canonical_source_paths_queries_and_robots(policy):
    original = "https://GitHub.COM.:443/thieu1995/%6dealpy/issues?a=1&x=é#discussion"
    allowed = policy.match(original)
    assert allowed.url == "https://github.com/thieu1995/mealpy/issues?a=1&x=%C3%A9"
    assert policy.robots_url(allowed).url == "https://github.com/robots.txt"
    with pytest.raises(SourcePolicyError):
        policy.match("https://github.com/robots.txt")
    with pytest.raises(SourcePolicyError):
        policy.match_robots("https://github.com/robots.txt?external=1", "github.com")
    with pytest.raises(SourcePolicyError):
        policy.match_robots("https://raw.githubusercontent.com/robots.txt", "github.com")


@pytest.mark.parametrize("url", [
    "http://github.com/thieu1995/mealpy",
    "https://github.com:444/thieu1995/mealpy",
    "https://evil@github.com/thieu1995/mealpy",
    "https://github.com.evil.test/thieu1995/mealpy",
    "https://raw.github.com/thieu1995/mealpy",
    "https://github.com/thieu1995/mealpy-malicious",
    "https://github.com/thieu1995/mealpy/../secrets",
    "https://github.com/thieu1995/mealpy/%2E%2e/secrets",
    "https://github.com/thieu1995/mealpy/%252e%252e/secrets",
    "https://github.com/thieu1995/mealpy/%2fsecrets",
    "https://github.com/thieu1995/mealpy/%EF%BC%8F",
    "https://github.com/thieu1995/mealpy/%C0%AF",
    "https://github.com/thieu1995/mealpy/..;foo/secrets",
    "https://github.com/thieu1995/mealpy?bad=%0d",
    "https://[::1]/thieu1995/mealpy",
    "https://2130706433/thieu1995/mealpy",
    "https://0x7f000001/thieu1995/mealpy",
])
def test_disallowed_document_targets(url, policy):
    with pytest.raises(SourcePolicyError) as error:
        policy.match(url)
    assert error.value.code == "SOURCE_NOT_ALLOWED"


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "169.254.169.254", "168.63.129.16",
    "100.100.100.200", "192.168.1.1", "239.1.2.3", "::", "::1", "::ffff:127.0.0.1",
    "fe80::1%en0", "fd00:ec2::254", "2002:7f00:0001::1", "64:ff9b::7f00:1",
])
def test_special_addresses_are_not_dialable(address):
    with pytest.raises(SourcePolicyError):
        require_public_ip(address)


def test_explicit_idna_and_subdomain_opt_in(policy):
    changed = policy.rules[0].model_dump() | {
        "hostname": "BÜCHER.test.",
        "include_subdomains": True,
    }
    new_policy = SourcePolicy(SourceSnapshot(revision=2, sources=[SourceRule.model_validate(changed)]))
    assert new_policy.match("https://api.bücher.test/thieu1995/mealpy").hostname == "api.xn--bcher-kva.test"
    assert new_policy.hash != policy.hash
    assert require_public_ip("2606:4700:4700:0::1111") == "2606:4700:4700::1111"
    assert require_public_ip("8.8.8.8") == "8.8.8.8"


def test_plan_dedup_preserves_scopes_and_purpose(policy):
    rule = policy.rules[0]
    other = SourceRule.model_validate(rule.model_dump() | {"source_id": "other", "hostname": "example.org"})
    snapshot = SourceSnapshot(revision=2, sources=[rule, other])
    q1 = {"query_id": "q1", "text": "solver  ＣＭＡ", "source_ids": [rule.source_id],
          "purpose": "method_discovery"}
    q2 = q1 | {"query_id": "q2", "text": "solver CMA", "source_ids": [other.source_id]}
    q3 = q1 | {"query_id": "q3", "text": "solver CMA"}
    plan = ResearchQueryPlan.model_validate({"schema_version": "2.0", "queries": [q1, q2, q3]})
    assert [query.query_id for query in plan.normalized(snapshot).queries] == ["q1", "q2"]
    with pytest.raises(ValidationError):
        ResearchQueryPlan.model_validate({"schema_version": "2.0", "queries": [q1, q1]})
    with pytest.raises(DomainError, match="configured query limit"):
        plan.normalized(snapshot, ResearchLimits(max_queries=1))
    with pytest.raises(DomainError, match="outside"):
        ResearchQueryPlan.model_validate({
            "schema_version": "2.0", "queries": [q1 | {"text": "test SITE:example.org"}],
        }).normalized(snapshot)
    with pytest.raises(DomainError, match="outside"):
        ResearchQueryPlan.model_validate({
            "schema_version": "2.0", "queries": [q1 | {"text": "test site:github.com/thieu1995/other"}],
        }).normalized(snapshot)


def test_duplicate_nonfinite_and_extra_payload_are_rejected(document_examples):
    with pytest.raises(ValueError):
        SourceRule.from_json(document_examples[0].replace('"enabled": true', '"enabled": true, "enabled": false'))
    with pytest.raises(ValueError):
        SourceRule.from_json(document_examples[0].replace('"enabled": true', '"enabled": NaN'))
    with pytest.raises(ValidationError):
        ResearchQueryPlan.from_json(document_examples[1].replace('"query_id":', '"headers": {}, "query_id":'))
    with pytest.raises(ValidationError):
        ResearchLimits(max_search_attempts=4)


def test_passage_must_resolve_to_exact_document_and_hash():
    extracted = "A verified passage."
    digest = hashlib.sha256(extracted.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    doc = SourceDocument(
        document_id="d1", source_id="example", requested_url="https://example.org/",
        final_url="https://example.org/", fetched_at=now, validated_at=now,
        content_type="text/plain", content_level="documentation", parser_version="test-v2",
        bytes_sha256=digest, text_sha256=digest, fetch_status="read",
    )
    passage = EvidencePassage(evidence_id="e1", document_id=doc.document_id, start=2, end=10, text="verified")
    passage.verify(doc, extracted)
    with pytest.raises(DomainError, match="does not resolve"):
        passage.verify(doc, extracted + " Changed document.")
    with pytest.raises(DomainError, match="does not resolve"):
        passage.verify(doc.model_copy(update={"document_id": "different"}), extracted)
    with pytest.raises(DomainError, match="does not resolve"):
        passage.verify(doc.model_copy(update={"content_level": "snippet"}), extracted)


def test_advisor_rejects_foreign_references_and_invalid_candidate_counts():
    implementations = (
        "mealpy:swarm_based.GWO:OriginalGWO",
        "mealpy:swarm_based.PSO:OriginalPSO",
        "mealpy:evolutionary_based.DE:OriginalDE",
    )
    candidates = [
        {"implementation_id": implementation, "config_id": f"config{i}", "rationale_fa": "Compatible",
         "card_ref": f"card{i}", "evidence_refs": ["e1"], "evidence_scope": "method_level"}
        for i, implementation in enumerate(implementations)
    ]
    data = {"schema_version": "2.0", "status": "ready", "candidates": candidates,
            "research_findings": [], "reason_codes": [], "uncertainty_notes": None}
    plan = AdvisorPlanV2.model_validate(data)
    cards = {row["card_ref"]: (row["implementation_id"], row["config_id"]) for row in candidates}
    plan.validate_references(cards, {"e1"}, require_web=True)
    with pytest.raises(DomainError) as error:
        plan.validate_references(cards, {"other_job"}, require_web=True)
    assert error.value.code == "EVIDENCE_REFERENCE_INVALID"
    with pytest.raises(DomainError) as error:
        plan.validate_references({}, {"e1"}, require_web=True)
    assert error.value.code == "IMPLEMENTATION_MAPPING_INVALID"
    with pytest.raises(ValidationError):
        AdvisorPlanV2.model_validate(data | {"candidates": [candidates[0]] * 3})
    with pytest.raises(ValidationError):
        AdvisorPlanV2.model_validate(data | {"status": "insufficient_evidence", "candidates": []})


def test_v2_policy_preserves_numeric_quotas():
    data = {
        "policy_version": "2.0", "portfolio_id": "continuous_catalog_v2",
        "selection_policy": "web_research_then_pilot", "max_objective_evaluations": 20_000,
        "max_compute_seconds": 120, "master_seed": 0, "explain_via_llm": False,
    }
    policy = ExecutionPolicyV2.from_input(ExecutionPolicyInputV2.model_validate(data))
    assert (policy.pilot_candidate_count, policy.main_evaluations, policy.max_llm_calls) == (3, 14_998, 8)
    with pytest.raises(ValidationError):
        ExecutionPolicyInputV2.model_validate(data | {"max_objective_evaluations": 5501})
    fixed = ExecutionPolicyV2.from_input(ExecutionPolicyInputV2.model_validate(data | {
        "selection_policy": "catalog_fixed", "max_objective_evaluations": 1002,
        "fixed_implementation_id": "mealpy:swarm_based.GWO:OriginalGWO", "fixed_config_id": "config",
    }))
    assert (fixed.pilot_candidate_count, fixed.main_evaluations, fixed.max_llm_calls) == (0, 500, 0)
