"""Closure presentation uses frozen facts and never regenerates numerical work."""
from copy import deepcopy
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from p_mc_optimizer.domain.models import TerminationReason
from p_mc_optimizer.llm.narrative_models import ReportEvidenceV3, REQUIRED_SECTIONS
from p_mc_optimizer.reporting import html_bytes
from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3, core_hash, evidence_hash, canonical_bytes
from p_mc_optimizer.reporting.narrative_v3 import apply_narrative_v3, fallback_sections, finalize_narrative
from p_mc_optimizer.reporting.presentation import (
    TERMINATION_LABELS, build_report_view_model, format_fact_value, format_value,
    render_view_model_html,
)

FIXTURES=Path(__file__).parent/'fixtures/narrative_v3'

def fixture(name='F1'):
    return json.loads((FIXTURES/(name+'.json')).read_text())

def prepare(report=None,version='1.1',*,accepted=None):
    report=fixture() if report is None else report
    evidence=build_evidence_v3(report,policy_version=version)
    payload=finalize_narrative(report,evidence,accepted or [],report_generation_policy_version=version)
    return apply_narrative_v3(report,payload)

def forbidden(*args,**kwargs):
    pytest.fail('Read-only report presentation attempted a provider, builder or numerical call')

@pytest.mark.parametrize('fact,value,unit,expected',[
    ('selection.relative_decrease_percent',17.8123,'percent','17.8 درصد'),
    ('pilot.m1.evaluations_per_repeat',500,'evaluations','500 ارزیابی'),
    ('solution.optimality_status','not_certified',None,'گواهی‌نشده'),
    ('solution.within_bounds',True,None,'داخل کران‌های ثبت‌شده'),
    ('solution.within_bounds',False,None,'خارج از کران‌های ثبت‌شده'),
    ('solution.origin_phase','preflight',None,'بررسی اولیه'),
    ('solution.objective_raw',1e-15,None,'1e-15'),
    ('solution.within_bounds',None,None,'ثبت نشده'),
])
def test_composable_fact_formatter_is_explicitly_versioned(fact,value,unit,expected):
    assert format_fact_value(fact,value,unit,version='1.1')==expected
    assert format_fact_value(fact,value,unit,version='1.0')==format_value(value,unit)
    assert value is None or 'درصد درصد' not in expected
    with pytest.raises(ValueError):format_fact_value(fact,value,unit,version='2.1')

def test_every_actual_termination_enum_has_a_readable_label_and_unknown_is_not_success():
    assert set(TERMINATION_LABELS)==set(get_args(TerminationReason))
    for value,label in TERMINATION_LABELS.items():
        assert format_fact_value('execution.termination_reason',value,version='1.1')==label
        assert value not in label
    assert format_fact_value('execution.termination_reason','new_future_enum',version='1.1')=='علت توقف با نگاشت نمایشی نامشخص'
    assert format_fact_value('execution.termination_reason','evaluation_budget',version='1.0')=='evaluation_budget'

def test_fallback_keeps_full_recorded_rationales_and_real_missing_data_in_shared_details(monkeypatch):
    from p_mc_optimizer.ui import results
    original=fixture();first=original['candidate_plan']['candidates'][0]
    first['rationale_fa']='<script>unsafe()</script> '+('دلیل ثبت‌شده با حدود شاهد. '*120)+' این شاهد برتری را ثابت نمی‌کند.'
    original['candidate_plan']['candidates'][1]['rationale_fa']=None
    before=deepcopy(original);report=prepare(original);narrative=report['narrative_v3'];evidence_before=evidence_hash(narrative['evidence'])
    vm=build_report_view_model(report);body=render_view_model_html(vm)
    assert vm['rationale_details'][0]['rationale']==first['rationale_fa']
    assert vm['rationale_details'][1]['rationale'] is None
    assert 'دلیلی برای این نامزد ثبت نشده است.' in body
    assert first['rationale_fa'] not in body and '&lt;script&gt;unsafe()&lt;/script&gt;' in body
    assert 'این شاهد برتری را ثابت نمی‌کند.' in body
    candidate=next(s for s in vm['sections'] if s['section_id']=='candidate_rationale')
    summary=' '.join(p['text_fa'] for p in candidate['paragraphs'])
    assert len(summary)<600 and first['rationale_fa'] not in summary
    assert 'دلایل ثبت‌شده پیشنهاد' in summary and 'دلیل ثبت‌شده ثبت نشده' not in summary
    assert 'proposal.rationale' not in {f['fact_id'] for f in narrative['evidence']['facts']}
    assert body in html_bytes(report).decode()
    captured=[];monkeypatch.setattr(results,'st',SimpleNamespace(markdown=lambda text,**kw:captured.append(text)))
    results._summary(report,{'job_status':'completed'},report['best_x'],report['objective_raw'],True,'closure')
    assert captured==[body]
    assert original==before and core_hash(original)==core_hash(report)
    assert evidence_hash(narrative['evidence'])==evidence_before

def test_display_citations_are_unique_per_section_without_modifying_raw_refs_or_hash():
    original=fixture();evidence=build_evidence_v3(original,policy_version='1.1')
    section=fallback_sections(original,evidence,['solution'])[0].model_dump(mode='json')
    paragraph=deepcopy(section['paragraphs'][0]);paragraph['paragraph_id']='second_paragraph'
    section['paragraphs'].append(paragraph)
    accepted={'section':section,'origin':'llm','source_draft_hash':'b'*64,'reviewer_invocation_id':'controlled-review'}
    digest=sha256(canonical_bytes(accepted)).hexdigest()
    report=prepare(original,accepted=[accepted]);vm=build_report_view_model(report)
    displayed=next(s for s in vm['sections'] if s['section_id']=='solution')
    refs=[ref['evidence_id'] for p in displayed['paragraphs'] for ref in p['references']]
    assert len(refs)==len(set(refs)) and refs==['report:solution']
    assert displayed['paragraphs'][0]['references'][0]['title']=='رکورد جواب و منشأ آن'
    retained=next(s for s in report['narrative_v3']['accepted_sections'] if s['origin']=='llm')
    assert retained==accepted and sha256(canonical_bytes(retained)).hexdigest()==digest

def test_historical_raw_sections_and_frozen_display_reopen_without_builder_or_external_calls(monkeypatch):
    import p_mc_optimizer.reporting.evidence_v3 as builder
    from p_mc_optimizer.llm.gateway import LLMGateway
    from p_mc_optimizer.execution.evaluator import BudgetedEvaluator
    from p_mc_optimizer.execution import supervisor
    original=fixture();evidence=build_evidence_v3(original)
    raw=evidence.model_dump(mode='json');next(f for f in raw['facts'] if f['fact_id']=='solution.x')['display_text']='[۱٫۴, −۲٫۲]'
    evidence=ReportEvidenceV3.model_validate(raw)
    accepted=[{'section':s.model_dump(mode='json'),'origin':'llm','source_draft_hash':'c'*64,'reviewer_invocation_id':'old-review'} for s in fallback_sections(original,evidence,REQUIRED_SECTIONS)]
    report=apply_narrative_v3(original,finalize_narrative(original,evidence,accepted,review_ran=True))
    before=deepcopy(report);report['narrative_v3']['rendered_sections'][3]['paragraphs'][0]['text_fa']='TAMPERED_RENDERED_TEXT'
    for target,name in [(builder,'build_evidence_v3'),(LLMGateway,'invoke'),(BudgetedEvaluator,'__call__'),(supervisor,'run_execution')]:monkeypatch.setattr(target,name,forbidden)
    vm=build_report_view_model(report);document=html_bytes(report).decode()
    solution=next(s for s in vm['sections'] if s['section_id']=='solution')
    assert '[۱٫۴, −۲٫۲]' in solution['paragraphs'][0]['text_fa'] and 'TAMPERED_RENDERED_TEXT' not in document
    assert vm['mode']==before['narrative_v3']['mode']=='llm'
    assert all(s['origin']=='llm' for s in vm['sections'])
    assert report['narrative_v3']['accepted_sections']==before['narrative_v3']['accepted_sections']
    assert report['narrative_v3']['formatter_version']=='1.0' and 'report_generation_policy_version' not in report
    assert core_hash(report)==core_hash(before)

@pytest.mark.parametrize('fault',['formatter','builder','unknown','marker','missing_marker','renderer'])
def test_unknown_or_mixed_versions_show_clear_server_fallback_without_upgrading_or_calls(monkeypatch,fault):
    import p_mc_optimizer.reporting.evidence_v3 as builder
    report=prepare();n=report['narrative_v3']
    if fault=='formatter':n['formatter_version']='1.0'
    elif fault=='builder':n['evidence_builder_version']='1.0'
    elif fault=='unknown':n['evidence']['evidence_policy_version']='9.9'
    elif fault=='marker':report['report_generation_policy_version']='1.0'
    elif fault=='renderer':n['renderer_version']='9.9'
    else:report.pop('report_generation_policy_version')
    before=deepcopy(report);monkeypatch.setattr(builder,'build_evidence_v3',forbidden)
    vm=build_report_view_model(report)
    assert vm['read_error'] and vm['mode']=='deterministic'
    assert all(s['origin']=='server' and not s['reviewed'] for s in vm['sections'])
    assert vm['read_error'] in html_bytes(report).decode()
    assert report==before

def test_finalize_marks_version_mismatch_invalid_and_uses_explicit_fallback_policy():
    original=fixture();old=build_evidence_v3(original)
    result=finalize_narrative(original,old,[],report_generation_policy_version='1.1')
    assert result['evidence_status']=='invalid' and result['evidence'] is None
    assert result['formatter_version']==result['evidence_builder_version']=='1.1'
    assert result['mode']=='deterministic' and any(i['code']=='CONTEXT_MISMATCH' for i in result['issues'])
    unknown=finalize_narrative(original,old,[],report_generation_policy_version='9.9')
    assert unknown['evidence_status']=='invalid' and unknown['formatter_version']=='1.0'

def test_effective_writer_and_reviewer_limits_are_taken_from_real_call_metadata():
    report=prepare();report['llm_profile']={'max_output_tokens':1234}
    report['llm_calls']=[{'role':'explainer','invocation_id':'draft','provenance':{'schema_id':'explainer.v3','effective_max_output_tokens':8192,'report_generation_policy_version':'1.1'}},
                         {'role':'report_reviewer','invocation_id':'review','provenance':{'schema_id':'narrative_review.v1','effective_max_output_tokens':4096,'report_generation_policy_version':'1.1'}}]
    vm=build_report_view_model(report);body=render_view_model_html(vm)
    assert [c['effective_max_output_tokens'] for c in vm['technical']['effective_output_limits']]==[8192,4096]
    assert 'سقف مؤثر خروجی نویسنده:' in body and 'سقف مؤثر خروجی بازبین:' in body
    assert '8192' in body and '4096' in body and '1234' not in body
    report['llm_calls']=[];body=html_bytes(report).decode()
    assert 'سقف مؤثر خروجی نویسنده و بازبین در دادهٔ تماس‌های این گزارش ثبت نشده است.' in body

@pytest.mark.parametrize('name',['F3','F4'])
def test_origin_and_selected_method_remain_separate_in_new_fallback_and_view(name):
    report=prepare(fixture(name));vm=build_report_view_model(report)
    text=' '.join(p['text_fa'] for s in vm['sections'] if s['section_id']=='solution' for p in s['paragraphs'])
    assert vm['selected_method']=='تکامل تفاضلی (DE)'
    if name=='F3':
        assert vm['origin_method']=='بهینه‌سازی نهنگ (WOA)' and 'پایلوت' in vm['origin_phase']
    else:
        assert vm['origin_phase']=='بررسی اولیه' and vm['origin_method']=='ثبت نشده'
        assert 'نام روش تولیدکننده در رکورد منشأ ثبت نشده است' in text
        selection=' '.join(p['text_fa'] for s in vm['sections'] if s['section_id']=='selection' for p in s['paragraphs'])
        assert 'رفع تساوی' in selection and 'برتری عددی را نشان نمی‌دهد' in selection
