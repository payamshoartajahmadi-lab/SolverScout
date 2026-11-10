"""Scope, policy and budget checks at the v2.1 request boundary."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from p_mc_optimizer.research.contracts import SourceRule, SourceSnapshot
from p_mc_optimizer.research.contracts_v21 import (
    CreateJobV21, ExecutionPolicyV21, LibrarySourceUpdate, ResearchQueryPlanV21,
    SourceSyncRequest, SyncLimits,
)
from p_mc_optimizer.research.source_policy import SourcePolicyError


def request(retrieval_mode="local_only"):
    return {
        "input": {"kind": "natural_language", "text": "TEST FIXTURE"},
        "llm_profile": {"profile_id": str(uuid4()), "revision": 1},
        "catalog_hash": "0" * 64,
        "source_revision": 3,
        "policy": {
            "policy_version": "2.1", "portfolio_id": "continuous_catalog_v2",
            "selection_policy": "evidence_research_then_pilot", "max_objective_evaluations": 5502,
            "max_compute_seconds": 120, "master_seed": 0, "explain_via_llm": False,
            "retrieval_mode": retrieval_mode,
        },
    }


def test_local_policy_is_independent_of_provider_and_keeps_trial_budget():
    payload = request()
    job = CreateJobV21.model_validate(payload)
    policy = ExecutionPolicyV21.from_input(job.policy)
    assert policy.main_evaluations == 500 and policy.max_llm_calls == 8
    assert policy.sync_limits is None and job.search_provider_ref is None
    assert CreateJobV21.from_json(job.model_dump_json()) == job
    with pytest.raises(ValidationError, match="provider"):
        CreateJobV21.model_validate(payload | {"search_provider_ref": payload["llm_profile"]})
    payload["policy"]["max_objective_evaluations"] -= 1
    with pytest.raises(ValidationError, match="5502"):
        CreateJobV21.model_validate(payload)


def test_mode_specific_inputs():
    direct = request("direct_refresh")
    with pytest.raises(ValidationError, match="Sync limits"):
        CreateJobV21.model_validate(direct)
    direct["policy"]["sync_limits"] = {}
    with pytest.raises(ValidationError, match="source configurations"):
        CreateJobV21.model_validate(direct)
    direct["source_config_refs"] = [{"source_id": "fixture", "revision": 1}]
    assert CreateJobV21.model_validate(direct).policy.sync_limits == SyncLimits()
    web = request("web_search")
    with pytest.raises(ValidationError, match="provider"):
        CreateJobV21.model_validate(web)
    web["search_provider_ref"] = web["llm_profile"]
    assert CreateJobV21.model_validate(web).search_provider_ref.revision == 1


def test_seed_and_sitemap_have_separate_scope():
    scope = SourceSnapshot(revision=3, sources=(SourceRule(
        source_id="fixture", label="TEST FIXTURE", hostname="example.org",
        path_prefixes=("/docs",), source_kind="official_docs", enabled=True,
    ),))
    update = LibrarySourceUpdate(
        expected_revision=0, seed_urls=["https://example.org/docs/index#top"],
        sitemap_urls=["https://example.org/sitemap.xml"],
    )
    config = update.to_config("fixture", 3).check_source(scope)
    assert config.seed_urls == ("https://example.org/docs/index",)
    assert config.sitemap_urls == ("https://example.org/sitemap.xml",)
    with pytest.raises(SourcePolicyError):
        config.model_copy(update={"seed_urls": config.sitemap_urls}).check_source(scope)
    with pytest.raises(SourcePolicyError):
        config.model_copy(update={"sitemap_urls": ("https://example.com/sitemap.xml",)}).check_source(scope)


def test_query_plan_retains_dynamic_catalog_targets_and_bounds_sync_request():
    queries = [
        {
            "query_id": "q1", "text": "TEST FIXTURE implementation", "source_ids": ["fixture"],
            "purpose": "implementation_evidence", "implementation_ids": ["mealpy:fixture:MyAlgo"],
        },
        {
            "query_id": "q2", "text": "TEST FIXTURE methods", "source_ids": ["fixture"],
            "purpose": "method_discovery",
        },
    ]
    plan = ResearchQueryPlanV21.model_validate({"schema_version": "2.1", "queries": queries})
    assert plan.queries[0].implementation_ids == ("mealpy:fixture:MyAlgo",)
    assert plan.for_web().queries[0].text == plan.queries[0].text
    assert "implementation_ids" not in plan.for_web().queries[0].model_dump()
    with pytest.raises(ValidationError):
        ResearchQueryPlanV21.model_validate({"schema_version": "2.1", "queries": queries[:1]})
    with pytest.raises(ValidationError, match="sync limit"):
        SourceSyncRequest(source_ids=["one", "two"], limits=SyncLimits(max_sources=1))
