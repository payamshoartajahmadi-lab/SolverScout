"""A read-only UI must not badge changed/stale evidence as reviewed model prose."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from p_mc_optimizer.llm.narrative_models import REQUIRED_SECTIONS
from p_mc_optimizer.reporting import html_bytes
from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3, evidence_hash, core_hash
from p_mc_optimizer.reporting.narrative_v3 import apply_narrative_v3, fallback_sections, finalize_narrative
from p_mc_optimizer.reporting.presentation import build_report_view_model, render_view_model_html


def prepared(version):
    source=json.loads((Path(__file__).parent/'fixtures/narrative_v3/F1.json').read_text())
    evidence=build_evidence_v3(source,policy_version=version)
    accepted=[{'section':s.model_dump(mode='json'),'origin':'llm','source_draft_hash':'a'*64,
               'reviewer_invocation_id':'CONTROLLED-READ-ONLY-TEST'}
              for s in fallback_sections(source,evidence,REQUIRED_SECTIONS)]
    return apply_narrative_v3(source,finalize_narrative(source,evidence,accepted,review_ran=True,
                              report_generation_policy_version=version))


def block_external_and_build(monkeypatch):
    def forbidden(*args,**kwargs):
        pytest.fail('Reopen attempted provider, EvidenceBuilder, evaluator or solver work')
    for path in ('p_mc_optimizer.reporting.evidence_v3.build_evidence_v3',
                 'p_mc_optimizer.llm.gateway.LLMGateway.invoke',
                 'p_mc_optimizer.llm.provider.call_provider',
                 'p_mc_optimizer.execution.evaluator.BudgetedEvaluator.__call__',
                 'p_mc_optimizer.execution.supervisor.run_execution'):
        monkeypatch.setattr(path,forbidden)


@pytest.mark.parametrize('version',['1.0','1.1'])
@pytest.mark.parametrize('fault',['display','context_hash','narrative_core','rehashed_evidence_core',
                                 'report_core','foreign_job','foreign_source_schema',
                                 'missing_evidence','invalid_evidence_flag'])
def test_reader_rejects_changed_or_foreign_bindings_without_mutation_or_calls(monkeypatch,version,fault):
    from p_mc_optimizer.ui import results
    report=prepared(version);n=report['narrative_v3'];e=n['evidence']
    if fault=='display':next(f for f in e['facts'] if f['fact_id']=='solution.objective_raw')['display_text']='999999'
    elif fault=='context_hash':n['context_hash']='0'*64
    elif fault=='narrative_core':n['source_core_sha256']='0'*64
    elif fault=='rehashed_evidence_core':
        e['source_core_sha256']='0'*64;n['context_hash']=evidence_hash(e)
    elif fault=='report_core':report['input']['text']+='\nاین ورودی پس از ثبت گزارش تغییر کرده است.'
    elif fault=='foreign_job':
        e['job_id']='33333333-3333-4333-8333-333333333333';n['context_hash']=evidence_hash(e)
    elif fault=='foreign_source_schema':
        e['source_report_schema_version']='other-report-schema';n['context_hash']=evidence_hash(e)
    elif fault=='missing_evidence':
        n.update(evidence=None,evidence_status='invalid',context_hash=None,source_core_sha256=None)
    else:n['evidence_status']='invalid'
    before=deepcopy(report);block_external_and_build(monkeypatch)
    view=build_report_view_model(report);body=render_view_model_html(view)
    assert view['read_error'] and view['mode']=='deterministic'
    assert all(s['origin']=='server' and not s['reviewed'] for s in view['sections'])
    assert body in html_bytes(report).decode() and view['read_error'] in body
    assert all('999999' not in p['text_fa'] for s in view['sections'] for p in s['paragraphs'])
    captured=[];monkeypatch.setattr(results,'st',SimpleNamespace(markdown=lambda text,**kw:captured.append(text)))
    results._summary(report,{'job_status':'completed'},report['best_x'],report['objective_raw'],True,'binding-reader')
    assert captured==[body] and report==before
    assert report['narrative_v3']['mode']=='llm' and report['narrative_v3']['review_status']=='passed'


@pytest.mark.parametrize('version',['1.0','1.1'])
def test_valid_frozen_projection_reopens_unchanged_and_ignores_only_render_cache(monkeypatch,version):
    report=prepared(version);before=deepcopy(report);expected=build_report_view_model(report)
    report['narrative_v3']['rendered_sections'][0]['paragraphs'][0]['text_fa']='TAMPERED CACHE'
    block_external_and_build(monkeypatch)
    view=build_report_view_model(report)
    assert not view['read_error'] and view['mode']=='llm'
    assert view['sections']==expected['sections'] and all(s['reviewed'] for s in view['sections'])
    assert 'TAMPERED CACHE' not in html_bytes(report).decode()
    assert report['narrative_v3']['accepted_sections']==before['narrative_v3']['accepted_sections']
    assert report['narrative_v3']['evidence']==before['narrative_v3']['evidence']
    assert core_hash(report)==core_hash(before)


def test_a_genuine_deterministic_invalid_evidence_fallback_is_still_readable(monkeypatch):
    original=prepared('1.1')
    report=apply_narrative_v3(original,finalize_narrative(original,None,[],evidence_invalid=True,
                             report_generation_policy_version='1.1'))
    before=deepcopy(report);block_external_and_build(monkeypatch)
    view=build_report_view_model(report)
    assert not view['read_error'] and view['mode']=='deterministic'
    assert all(s['origin']=='server' for s in view['sections'])
    assert render_view_model_html(view) in html_bytes(report).decode() and report==before
