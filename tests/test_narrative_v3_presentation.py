"""Shared v3 rendering, useful fallbacks and numerical immutability."""
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from p_mc_optimizer.reporting import html_bytes
from p_mc_optimizer.reporting.presentation import (
    format_value, format_full_value, method_display_name, comparison_rows,
    build_report_view_model, render_view_model_html, SECTION_ORDER,
)
from p_mc_optimizer.reporting.narrative_v3 import finalize_narrative, apply_narrative_v3, fallback_sections
from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3, core_hash

FIXTURE = Path(__file__).parent / "fixtures/narrative_v3/F1.json"


def report():
    return json.loads(FIXTURE.read_text())


def prepared(*, model_sections=False):
    original = report()
    evidence = build_evidence_v3(original)
    accepted = []
    if model_sections:
        accepted = [{"section": s.model_dump(mode="json"), "origin": "llm", "source_draft_hash": "a"*64,
                     "reviewer_invocation_id": "reviewed-fixture-invocation"}
                    for s in fallback_sections(original, evidence, SECTION_ORDER[:5])]
    narrative = finalize_narrative(original, evidence, accepted, elapsed_reporting_seconds=0.01,
                                   review_ran=model_sections)
    return apply_narrative_v3(original, narrative)


@pytest.mark.parametrize("value,unit,expected", [
    (0.0,None,"0"),(1e-15,None,"1e-15"),(14998,None,"14998"),
    (0.00016715369554070285,None,"0.0001671537"),
    (17.8123,"percent","17.8"),(100,"percent","100.0"),
    (1.23456,"seconds","1.23"),(12345,"seconds","1.23e+04"),
    (None,None,"ثبت نشده"),([-5.0,5.0],None,"[-5, 5]"),
])
def test_shared_formatter_preserves_small_values_and_declared_units(value,unit,expected):
    """N3-10/30: presentation never rounds a nonzero stored objective to zero."""
    assert format_value(value,unit)==expected
    if type(value) is float:
        assert float(format_full_value(value)) == value


def test_methods_are_readable_without_leaking_internal_identifiers():
    """N3-31."""
    assert method_display_name("mealpy:evolutionary_based.DE:OriginalDE") == "تکامل تفاضلی (DE)"
    assert "گرگ خاکستری" in method_display_name("mealpy:swarm_based.GWO:OriginalGWO")
    assert method_display_name("mealpy:evolutionary_based.DE:JADE").startswith("تکامل تفاضلی (DE)")
    assert method_display_name("mealpy:unsafe:<script>","m4") == "روش ثبت‌شده (m4)"
    assert "mealpy:" not in method_display_name("mealpy:new.Unknown:NewClass")


def test_actual_summary_table_uses_raw_medians_and_true_ranking():
    """N3-01/02/30: metric array order is not the ranking."""
    original=report()
    rows=comparison_rows(original)
    assert [row['implementation_id'].split(':')[-1] for row in rows] == ['OriginalDE','OriginalWOA','OriginalGWO']
    assert [row['rank'] for row in rows] == [1,2,3]
    assert all(row['valid_repeats']==3 and row['evaluation_counts']==[500]*3 for row in rows)
    shuffled=deepcopy(original);shuffled['decision']['metrics'].reverse()
    assert comparison_rows(shuffled)==rows
    fixed=deepcopy(original);fixed['decision']['decision_source']='fixed_policy'
    assert comparison_rows(fixed)==[]


def test_fallback_contains_actual_numbers_and_preserves_every_numeric_field():
    """N3-21/26: useful fallback is data-specific and does not rerun execution."""
    original=report();before=deepcopy(original);evidence=build_evidence_v3(original)
    value=apply_narrative_v3(original,finalize_narrative(original,evidence,[],review_ran=False))
    assert core_hash(value)==core_hash(original)
    assert original==before
    assert value['narrative_v3']['mode']=='deterministic'
    assert value['narrative_v3']['review_status']=='not_run'
    assert value['narrative_v3']['omitted_sections']==['mathematical_analysis']
    text=' '.join(p['text_fa'] for s in value['narrative_v3']['rendered_sections'] for p in s['paragraphs'])
    assert '14998' in text and '20000' in text
    assert '0.0001671537' in text and '0.0002033012' in text
    assert 'mealpy:' not in text
    assert all(item['origin']=='server' for item in value['narrative_v3']['accepted_sections'])
    assert value['explanation_notes']==[] and value['explanation_contract_version']=='3.0'
    assert value['explanation_evidence']['schema_version']=='3.0'
    assert 'note_selection_source' not in value['explanation_evidence']


def test_mixed_fallback_keeps_accepted_section_byte_identical_and_removes_unreviewed_math():
    """N3-17/20: only the rejected parts are replaced."""
    original=report();evidence=build_evidence_v3(original)
    first=fallback_sections(original,evidence,['solution'])[0].model_dump(mode='json')
    accepted={'section':first,'origin':'llm','source_draft_hash':'b'*64,'reviewer_invocation_id':'review-one'}
    result=finalize_narrative(original,evidence,[accepted],review_ran=True)
    assert result['mode']=='mixed' and result['review_status']=='partial'
    retained=next(item for item in result['accepted_sections'] if item['section']['section_id']=='solution')
    assert retained==accepted
    assert all(item['origin']=='server' for item in result['accepted_sections'] if item is not retained and item['section']['section_id']!='solution')
    assert 'mathematical_analysis' in result['omitted_sections']
    unreviewed=deepcopy(accepted);unreviewed['reviewer_invocation_id']=None
    assert finalize_narrative(original,evidence,[unreviewed])['mode']=='deterministic'


class ParagraphParser(HTMLParser):
    def __init__(self):super().__init__();self.current=None;self.values={}
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='p' and 'data-paragraph' in attrs:
            self.current=attrs['data-paragraph'];self.values[self.current]=''
    def handle_endtag(self,tag):
        if tag=='p':self.current=None
    def handle_data(self,data):
        if self.current:self.values[self.current]+=data


def test_ui_and_html_use_identical_view_model_and_plain_text(monkeypatch):
    """N3-26/30: render/export/reopen are pure and share exactly the same content."""
    from p_mc_optimizer.ui import results
    from p_mc_optimizer.execution.evaluator import BudgetedEvaluator
    from p_mc_optimizer.execution import supervisor
    def forbidden(*a,**kw):pytest.fail('Rendering attempted a numerical call')
    monkeypatch.setattr(BudgetedEvaluator,'__call__',forbidden)
    monkeypatch.setattr(supervisor,'run_execution',forbidden)
    value=prepared(model_sections=True);before=deepcopy(value);digest=core_hash(value)
    vm=build_report_view_model(value);body=render_view_model_html(vm);document=html_bytes(value).decode()
    assert body in document
    captured=[];monkeypatch.setattr(results,'st',SimpleNamespace(markdown=lambda body,**kwargs:captured.append(body)))
    results._summary(value,{'job_status':'completed'},value['best_x'],value['objective_raw'],True,'unused')
    assert captured==[body]
    parser=ParagraphParser();parser.feed(document)
    expected={p['paragraph_id']:p['text_fa'] for s in vm['sections'] for p in s['paragraphs']}
    assert parser.values==expected
    assert [section['section_id'] for section in vm['sections']]==list(SECTION_ORDER[:5])
    assert document.count('بهینگی سراسری در این اجرا اثبات نشده است.')==1
    assert '<bdi dir="ltr">' in document
    assert core_hash(value)==digest and value==before
    assert build_report_view_model(value)==vm


def test_model_and_source_markup_are_escaped_and_unregistered_urls_are_never_links():
    """N3-32: even corrupted stored rendered text cannot run as markup."""
    value=prepared();n=value['narrative_v3']
    p=n['accepted_sections'][0]['section']['paragraphs'][0]
    p['text_fa']='<script>alert("unsafe")</script> '+p['text_fa']
    n['rendered_sections'][0]['paragraphs'][0]['text_fa']='<iframe src="https://evil.example"></iframe>'
    doc=html_bytes(value).decode()
    assert '<script>' not in doc and '<iframe' not in doc
    assert '&lt;script&gt;' in doc
    assert 'href="https://evil.example"' not in doc
    assert 'font-size:16px' in doc and 'line-height:1.8' in doc


def test_maximization_presentation_keeps_objective_and_score_distinct():
    """N3-04/30: explicit independent display columns."""
    value=json.loads(FIXTURE.with_name("F2.json").read_text())
    rows=comparison_rows(value)
    assert rows[0]['median_objective_raw']>999
    assert rows[0]['median_score']<0


def test_progress_chart_uses_recorded_fe_and_score_in_the_shared_presentation(monkeypatch):
    """Restore the real numeric chart in v3, never model markup or a placeholder."""
    from p_mc_optimizer.ui import results
    value=prepared()
    value['progress']=[{'started_evaluations':10,'score_for_minimization':4.0},
                       {'started_evaluations':20,'score_for_minimization':-2.0}]
    before=deepcopy(value);digest=core_hash(value)
    vm=build_report_view_model(value)
    assert vm['progress']==value['progress']
    body=render_view_model_html(vm);document=html_bytes(value).decode()
    # At FE=10 the chart is halfway across its 600-unit x range; the two
    # recorded extrema fill the 175-unit y range. The step is actual history.
    assert '<svg' in body and '406.00,20.00 706.00,20.00 706.00,195.00' in body
    assert '>4</text>' in body and '>-2</text>' in body and '>20</text>' in body
    assert body.index('data-block="usage"')<body.index('data-block="progress"')
    assert body in document
    captured=[];monkeypatch.setattr(results,'st',SimpleNamespace(markdown=lambda body,**kwargs:captured.append(body)))
    results._summary(value,{'job_status':'completed'},value['best_x'],value['objective_raw'],True,'unused')
    assert captured==[body]
    assert value==before and core_hash(value)==digest
    value['progress']=[{'started_evaluations':'<script>','score_for_minimization':1},
                       {'started_evaluations':2,'score_for_minimization':float('inf')}]
    assert build_report_view_model(value)['progress']==[]
    assert '<svg' not in html_bytes(value).decode()


def test_fixed_origin_and_identifier_tie_fixtures_have_honest_presentation():
    """N3-05/06: selected algorithm is independent of the record that made the best point."""
    original=json.loads(FIXTURE.with_name("F3.json").read_text())
    evidence=build_evidence_v3(original)
    value=apply_narrative_v3(original,finalize_narrative(original,evidence,[]))
    vm=build_report_view_model(value)
    assert vm['selected_method']=='تکامل تفاضلی (DE)'
    assert vm['origin_method']=='بهینه‌سازی نهنگ (WOA)' and vm['origin_phase']=='پایلوت'
    assert vm['objective_raw']==0
    tied=json.loads(FIXTURE.with_name("F4.json").read_text())
    rows=comparison_rows(tied)
    assert all(row['median_score']==row['normalized_iqr']==0 for row in rows)
    assert [row['implementation_id'] for row in rows]==sorted(row['implementation_id'] for row in rows)
    evidence=build_evidence_v3(tied)
    value=apply_narrative_v3(tied,finalize_narrative(tied,evidence,[]))
    text=' '.join(p['text_fa'] for section in value['narrative_v3']['rendered_sections'] for p in section['paragraphs'])
    assert 'برتری عددی را نشان نمی‌دهد' in text


def test_invalid_core_and_ineligible_status_never_republish_an_accepted_old_draft():
    """N3-23: old reviewed words cannot survive invalid/currently ineligible Evidence."""
    old=prepared(model_sections=True)
    original=report();evidence=build_evidence_v3(original)
    accepted=old['narrative_v3']['accepted_sections']
    bad=finalize_narrative(original,evidence,accepted,evidence_invalid=True)
    assert bad['mode']=='deterministic' and bad['evidence_status']=='invalid'
    assert bad['context_hash'] is bad['source_core_sha256'] is bad['evidence'] is None
    assert all(item['origin']=='server' for item in bad['accepted_sections'])
    stopped=finalize_narrative(original,evidence,accepted,requested=False)
    assert stopped['mode']=='deterministic'
    assert all(item['origin']=='server' for item in stopped['accepted_sections'])
