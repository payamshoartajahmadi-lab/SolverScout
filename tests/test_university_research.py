"""Offline six-card evidence controls; these fixtures are not live-model results."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from p_mc_optimizer.llm.research_v2 import PlannerContextV21, validate_output
from p_mc_optimizer.research.contracts import SearchProblemProfile, SourceRule
from p_mc_optimizer.research.contracts_v21 import ResearchQueryPlanV21
from p_mc_optimizer.research.extract_worker import parse
from p_mc_optimizer.research.library_retrieval import has_target_evidence, target_sections
from test_library_v21_retrieval import library, _entry, _job


TARGETS = {
    "mealpy:swarm_based.ABC:OriginalABC": "Artificial bee colony scouts abandon exhausted food sources.",
    "mealpy:swarm_based.PSO:OriginalPSO": "Particle swarm has local and global acceleration coefficients.",
    "mealpy:swarm_based.GWO:OriginalGWO": "Grey wolf optimizer compares a population of candidate vectors.",
    "mealpy:swarm_based.WOA:OriginalWOA": "Whale optimization updates a population of candidate vectors.",
    "mealpy:evolutionary_based.DE:OriginalDE": "Differential evolution mutates differences between population vectors.",
    "mealpy:evolutionary_based.ES:CMA_ES": "Covariance adaptation adjusts the distribution of candidate vectors.",
}


def html_classes(items, *, filler=0):
    toc = "".join(f'<a href="#{identifier.replace(":", ".")}">{identifier.rsplit(":", 1)[-1]}</a>' for identifier in items)
    result = [f'<nav><div><div>Contents {toc}</div></div></nav><main><p>{"prefix " * filler}</p>']
    for identifier, description in items.items():
        anchor = identifier.replace(":", ".")
        result.append(f'<dl class="py class"><dt id="{anchor}">class {anchor}(epoch=1000)</dt>'
                      f'<dd><p>{description}</p><p>Population initialization uses bounded real variables.</p></dd></dl>')
    return ("".join(result) + "</main>").encode()


def test_api_sections_beyond_prefix_limit_are_kept_and_navigation_is_not_evidence():
    custom = {"other:arbitrary.Module:FutureOptimizer": "A future class description with a new independent characteristic."}
    parsed = parse(html_classes(custom, filler=2000), "text/html", "documentation", 300)
    text = parsed["text"]
    assert len(text) <= 300
    assert "Contents" not in text and "prefix" not in text
    identifier, description = next(iter(custom.items()))
    assert description in text
    assert has_target_evidence(text, identifier)
    assert not has_target_evidence("Contents FutureOptimizer", identifier)
    section, = target_sections(text, identifier)
    assert section["anchor"] == identifier.replace(":", ".")
    assert text[section["start"]:section["end"]].startswith("## " + section["anchor"])


def test_six_dedicated_targets_include_four_independent_classes_in_one_document(library):
    store, retriever, source = library
    texts = {}
    for group, items in (("swarm", dict(list(TARGETS.items())[:4])), ("evolutionary", dict(list(TARGETS.items())[4:]))):
        text = parse(html_classes(items, filler=2000), "text/html", "documentation", 5000)["text"]
        url = f"https://methods.example.test/manual/{group}"
        texts[url] = text
        store.publish_documents([_entry(url, group, text)], source)
    plan = {"schema_version": "2.1", "queries": [
        {"query_id": "all-methods", "purpose": "implementation_evidence", "text": "bounded continuous optimization",
         "source_ids": ["methods"], "implementation_ids": list(TARGETS)},
        {"query_id": "discovery", "purpose": "method_discovery", "text": "other optimization methods",
         "source_ids": ["methods"], "implementation_ids": []},
    ]}
    aliases = {identifier: (identifier.rsplit(":", 1)[-1],) for identifier in TARGETS}
    result = retriever.retrieve(_job(store), plan, source, aliases=aliases)
    assert result["status"] == "completed"
    assert len(result["evidence"]) <= 12
    assert sum(len(row["text"]) for row in result["evidence"]) <= 18_000
    assert result["target_coverage"]["dedicated_implementation_ids"] == sorted(TARGETS)
    assert result["target_coverage"]["missing_dedicated_implementation_ids"] == []
    for identifier, description in TARGETS.items():
        passage = next(row for row in result["evidence"] if row["section"] == identifier.replace(":", "."))
        assert description in passage["text"]
        assert passage["text"] == texts[passage["url"]][passage["start"]:passage["end"]]
        document = next(row for row in result["documents"] if row["document_id"] == passage["document_id"])
        assert document["text_sha256"] == hashlib.sha256(texts[passage["url"]].encode()).hexdigest()
    assert sum(row["url"].endswith("/swarm") for row in result["evidence"]) >= 4


def test_planner_must_cover_six_cards_without_silently_rewriting_the_model_plan():
    context = PlannerContextV21(
        profile=SearchProblemProfile(n_variables=2, objective_sense="minimize"),
        sources=[SourceRule(source_id="methods",label="Fixture",hostname="example.org",path_prefixes=["/docs"],
                            source_kind="official_docs",enabled=True)], max_queries=3, retrieval_mode="local_only",
        solver_cards=[{"implementation_id":identifier,"family_id":identifier.split(":")[1].split(".")[-1],
                       "name":identifier.rsplit(":",1)[-1],"aliases":[]} for identifier in TARGETS],
    )
    raw = {"schema_version":"2.1","queries":[
        {"query_id":"all","purpose":"implementation_evidence","text":"method characteristics","source_ids":["methods"],"implementation_ids":list(TARGETS)},
        {"query_id":"other","purpose":"method_discovery","text":"other methods","source_ids":["methods"],"implementation_ids":[]}]}
    plan=ResearchQueryPlanV21.model_validate(raw)
    validate_output("research_planner.v2.1",plan,context)
    incomplete=plan.model_copy(update={"queries":(plan.queries[0].model_copy(update={"implementation_ids":tuple(list(TARGETS)[:3])}),plan.queries[1])})
    before=incomplete.model_dump_json()
    with pytest.raises(ValueError,match="missing implementation_ids"):
        validate_output("research_planner.v2.1",incomplete,context)
    assert incomplete.model_dump_json()==before


def test_six_card_advisor_context_keeps_three_mapped_nominees_without_numeric_execution():
    """Controlled context/mapping test, not proof of genuine qualification or model quality."""
    from p_mc_optimizer.catalog.adapter import resolve_configuration
    from p_mc_optimizer.problems.registry import freeze_problem
    from p_mc_optimizer.domain.models import ProblemInput
    from p_mc_optimizer.workflow.catalog_v2 import CatalogWorkflow

    cards = [{"implementation_id": identifier, "config_id": resolve_configuration(identifier).config_id,
              "family_id": identifier.split(":")[1].split(".")[-1]} for identifier in TARGETS]
    passages = []
    for index, (identifier, description) in enumerate(TARGETS.items()):
        text = identifier.rsplit(":", 1)[-1] + " " + description
        passages.append({"schema_version":"2.1", "evidence_id":str(index), "document_id":str(index),
            "version_id":str(index), "observation_id":str(index), "source_id":"methods", "url":"https://example.org/docs",
            "selection_method":"implementation_evidence", "text":text, "start":0, "end":len(text)})
    profile = {"n_variables":2,"lower":[-5.,-5.],"upper":[5.,5.],"widths":[10.,10.],"objective_sense":"minimize",
               "sample_count":500,"objective_scale":1.,"score_min":0.,"score_median":1.,"score_max":2.,
               "normalized_iqr":1.,"normalized_std":1.,"mean_evaluation_seconds":0.001}
    problem = json.loads((Path(__file__).parents[1]/"examples/quadratic_2d.json").read_text())

    class Driver(CatalogWorkflow):
        def __init__(self):
            self.data={"solver_cards":cards,"profile":profile,"research_outcome":{"snapshot_id":"fixture","evidence":passages}}
            self.repo=SimpleNamespace(record_research_findings=lambda *args,**kwargs:None)
            self.job_id="fixture";self.worker_id="fixture";self.seen=None

        @property
        def workflow(self): return self.data

        @property
        def policy(self): return SimpleNamespace(policy_version="2.1",selection_policy="evidence_research_then_pilot")

        @property
        def revision(self): return freeze_problem(ProblemInput.model_validate(problem))

        def keep(self,key,value): self.data[key]=value.model_dump(mode="json") if hasattr(value,"model_dump") else value

        def _role(self,role,schema,context,validate):
            self.seen=context.model_dump(mode="json")
            output={"schema_version":"2.1","status":"ready","reason_codes":[],"research_findings":[],"uncertainty_notes":None,
                "candidates":[{"implementation_id":row["implementation_id"],"config_id":row["config_id"],
                               "card_ref":row["implementation_id"],"rationale_fa":"کنترل قراردادی آفلاین",
                               "evidence_scope":"method_level","evidence_refs":[str(index)]}
                              for index,row in enumerate(cards[:3])]}
            return validate(output),None

    driver=Driver()
    driver.propose_candidates()
    assert {row["implementation_id"] for row in driver.seen["solver_cards"]} == set(TARGETS)
    assert len(driver.seen["solver_cards"])==6
    assert len(driver.data["candidate_plan"]["candidates"])==3
    assert {row["algorithm_id"] for row in driver.data["candidate_plan"]["candidates"]} <= set(TARGETS)
