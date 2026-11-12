"""Free prose contracts, bindings, partial failures and review request leases."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from p_mc_optimizer.llm.models import provider_schema
from p_mc_optimizer.llm.narrative_models import *
from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3,evidence_hash,canonical_bytes
from p_mc_optimizer.reporting.narrative_validation import *

FIXTURE=Path(__file__).parent/'fixtures/narrative_v3/F1.json'
def evidence():return build_evidence_v3(json.loads(FIXTURE.read_text()))
def context():
 e=evidence();d={'schema_version':'3.0','mode':'draft','prompt_bundle_sha256':'1'*64,'evidence':e.model_dump(mode='json'),'context_hash':evidence_hash(e),'request_hash':'0'*64,'section_scope':list(SECTION_ORDER),'accepted_sections':[],'failed_sections':[],'previous_response_text':None,'previous_response_truncated':False,'repair_issues':[]};d['request_hash']=request_hash(d);return WriterContextV3.model_validate(d)
def draft(c,variant=0):
 sections=[]
 for s in c.evidence.sections:
  if not s.required:continue
  refs=s.required_fact_refs
  text=('شواهد این بخش چنین است: ' if variant==0 else 'در این اجرا می‌توان داده‌های ثبت‌شده را چنین خواند: ')+ '؛ '.join('{{fact:'+f+'}}' for f in refs)+'.'
  sections.append({'section_id':s.section_id,'paragraphs':[{'paragraph_id':s.section_id,'claim_kind':'recorded','text_fa':text,'fact_refs':refs,'evidence_refs':['report:problem' if s.section_id=='problem' else 'report:decision' if s.section_id=='selection' else 'report:solution' if s.section_id=='solution' else 'report:budget' if s.section_id=='verification_and_limits' else 'report:proposal']}]})
 return {'schema_version':'3.0','context_hash':c.context_hash,'request_hash':c.request_hash,'sections':sections}
def review_context(c,sections,accepted=(),scope=None):
 d={'schema_version':'1.0','prompt_bundle_sha256':c.prompt_bundle_sha256,'evidence':c.evidence.model_dump(mode='json'),'context_hash':c.context_hash,'request_hash':'0'*64,'draft_hash':draft_hash(c.context_hash,sections),'draft_sections':[s.model_dump(mode='json') for s in sections],'rendered_sections':[s.model_dump(mode='json') for s in render_sections(c.evidence,sections)],'section_scope':[s.section_id for s in sections] if scope is None else scope,'accepted_section_hashes':[{'section_id':s.section_id,'sha256':section_hash(s)} for s in accepted]};d['request_hash']=request_hash(d);return ReviewerContextV1.model_validate(d)
def accepted_review(c):return {'schema_version':'1.0','context_hash':c.context_hash,'request_hash':c.request_hash,'draft_hash':c.draft_hash,'section_reviews':[{'section_id':s,'verdict':'accept','issues':[]} for s in c.section_scope]}

@pytest.mark.parametrize('variant',[0,1])
def test_two_different_free_prose_drafts_pass_without_matching_server_sentences(variant):
 c=context();r=validate_draft(c,draft(c,variant));assert not r.global_error and r.issues==[] and len(r.valid_sections)==5

@pytest.mark.parametrize('fault,code',[('context_hash','CONTEXT_MISMATCH'),('request_hash','REQUEST_MISMATCH')])
def test_foreign_context_or_request_hash_rejected_globally(fault,code):
 c=context();d=draft(c);d[fault]='a'*64;r=validate_draft(c,d)
 assert r.global_error and r.valid_sections==[] and r.issues[0].code==code

@pytest.mark.parametrize('text,code',[('مصرف ۲۰۰۰۰ ارزیابی است.','UNBOUND_NUMBER'),('مصرف 2e4 ارزیابی است.','UNBOUND_NUMBER'),('{{fact:foreign.x}}','UNKNOWN_FACT'),('{{fact:}}','INVALID_BINDING'),('<script>alert()</script>','UNSAFE_MARKUP'),('https://evil.example','UNSAFE_MARKUP'),('os.system("echo bad")','UNSAFE_MARKUP')])
def test_bad_local_paragraph_preserves_other_valid_sections(text,code):
 c=context();d=draft(c);d['sections'][2]['paragraphs'][0]['text_fa']+=' '+text
 r=validate_draft(c,d);assert not r.global_error
 assert code in [i.code for i in r.issues] and len(r.valid_sections)==4
 assert all(i.section_id=='selection' for i in r.issues)

def test_missing_fact_coverage_and_unknown_reference_are_local():
 c=context();d=draft(c);p=d['sections'][0]['paragraphs'][0];p['text_fa']='متن عمومی';p['fact_refs']=[];p['evidence_refs']=['report:another-job']
 r=validate_draft(c,d);assert {i.code for i in r.issues}>={'MISSING_REQUIRED_FACT','UNKNOWN_EVIDENCE'} and len(r.valid_sections)==4

def test_resolver_performs_only_one_plain_text_substitution():
 c=context();e=c.evidence.model_dump(mode='json');f=next(f for f in e['facts'] if f['fact_id']=='problem.expression');f['display_text']='{{fact:constant.zero}} <b>منبع</b>'
 e=ReportEvidenceV3.model_validate(e);s=NarrativeSectionV3(section_id='problem',paragraphs=[NarrativeParagraphV3(paragraph_id='once',claim_kind='recorded',text_fa='{{fact:problem.expression}}',fact_refs=['problem.expression'],evidence_refs=['report:problem'])])
 assert render_sections(e,[s])[0].paragraphs[0].text_fa=='{{fact:constant.zero}} <b>منبع</b>'

def test_registered_variable_digits_are_allowed_but_random_variables_are_unbound():
 c=context();d=draft(c);d['sections'][0]['paragraphs'][0]['text_fa']+=' متغیر x1 و x2 ثبت شده‌اند.'
 assert not validate_draft(c,d).issues
 d['sections'][0]['paragraphs'][0]['text_fa']+=' x99';assert 'UNBOUND_NUMBER' in [i.code for i in validate_draft(c,d).issues]

def test_optional_mathematical_analysis_is_interpretation_and_constant_bound():
 c=context();d=draft(c);d['sections'].append({'section_id':'mathematical_analysis','paragraphs':[{'paragraph_id':'math','claim_kind':'interpretation','text_fa':'فرمول {{fact:problem.expression}} را می‌توان با کران نظری {{fact:constant.zero}} تفسیر کرد.','fact_refs':['problem.expression','constant.zero'],'evidence_refs':['report:problem']}]})
 assert not validate_draft(c,d).issues
 d['sections'][-1]['paragraphs'][0]['claim_kind']='recorded';assert validate_draft(c,d).issues[-1].code=='SCHEMA_INVALID'

def test_correct_negation_and_false_claim_are_not_keyword_blocked_by_deterministic_validator():
 c=context();d=draft(c);d['sections'][-1]['paragraphs'][0]['text_fa']+=' بهینگی سراسری تضمین نمی‌شود. با وجود این، برنامه گواه رسمی بهینگی داد.'
 assert validate_draft(c,d).issues==[] # Semantic reviewer, not a keyword blacklist, owns this judgement.

def test_repair_is_only_target_sections_and_accepted_hashes_cannot_change():
 c=context();initial=validate_draft(c,draft(c));data=c.model_dump(mode='json');data.update(mode='repair',section_scope=['selection'],accepted_sections=[s.model_dump(mode='json') for s in initial.valid_sections if s.section_id!='selection']);data['request_hash']=request_hash(data);repair=WriterContextV3.model_validate(data)
 d=draft(c);d.update(context_hash=repair.context_hash,request_hash=repair.request_hash);d['sections']=[s for s in d['sections'] if s['section_id']=='selection']
 assert not validate_draft(repair,d).issues
 d['sections'].append(initial.valid_sections[0].model_dump(mode='json'));assert validate_draft(repair,d).issues[0].code=='OUT_OF_SCOPE_REPAIR'

def test_optional_math_repair_can_omit_it_without_an_empty_accepted_section():
 c=context();data=c.model_dump(mode='json');data.update(mode='repair',section_scope=['mathematical_analysis']);data['request_hash']=request_hash(data);c=WriterContextV3.model_validate(data)
 d={'schema_version':'3.0','context_hash':c.context_hash,'request_hash':c.request_hash,'sections':[{'section_id':'mathematical_analysis','paragraphs':[]}]};r=validate_draft(c,d)
 assert r.issues==[] and r.valid_sections==[] and r.omitted_sections==['mathematical_analysis']

@pytest.mark.parametrize('fault',['hash','scope','accept_with_issue','reject_without_issue','foreign_paragraph','foreign_evidence'])
def test_review_contract_and_binding_are_strict(fault):
 c=context();sections=validate_draft(c,draft(c)).valid_sections;rc=review_context(c,sections);r=accepted_review(rc)
 if fault=='hash':r['draft_hash']='a'*64
 elif fault=='scope':r['section_reviews'].pop()
 elif fault=='reject_without_issue':r['section_reviews'][0]['verdict']='reject'
 else:
  r['section_reviews'][0]['issues']=[{'section_id':'problem','paragraph_id':'foreign' if fault=='foreign_paragraph' else 'problem','code':'CONTRADICTS_EVIDENCE','evidence_refs':['source:foreign'] if fault=='foreign_evidence' else ['report:problem'],'message_fa':'این ادعا پشتیبانی نشده است.','required_change_fa':'اصلاح شود.'}]
  if fault!='accept_with_issue':r['section_reviews'][0]['verdict']='reject'
 with pytest.raises(NarrativeValidationError):validate_review(rc,r)

def test_review_resolved_text_and_accepted_section_hash_are_verified():
 c=context();sections=validate_draft(c,draft(c)).valid_sections;rc=review_context(c,sections,accepted=sections[:1]);r=accepted_review(rc)
 assert validate_review(rc,r).section_reviews[0].verdict=='accept'
 data=rc.model_dump(mode='json');data['rendered_sections'][0]['paragraphs'][0]['text_fa']='دادهٔ دیگر';data['request_hash']=request_hash(data)
 with pytest.raises(NarrativeValidationError):validate_review(data,r)

def test_envelopes_and_required_nulls_are_closed():
 for model in [ReportEvidenceV3,NarrativeDraftV3,NarrativeReviewV1,WriterContextV3,ReviewerContextV1]:
  schema=provider_schema(model)
  for obj in [schema,*schema.get('$defs',{}).values()]:
   if obj.get('type')=='object':assert obj['additionalProperties'] is False and set(obj['required'])==set(obj['properties'])
 c=context();raw=c.model_dump(mode='json');raw.pop('previous_response_text')
 with pytest.raises(ValidationError):WriterContextV3.model_validate(raw)

def _qa_module():
 import importlib.util
 spec=importlib.util.spec_from_file_location('qa_narrative_v3',Path(__file__).parents[1]/'scripts/qa_narrative_v3.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def test_campaign_unknown_attempts_are_persistently_charged_and_replay_is_idempotent(tmp_path):
 qa=_qa_module();path=tmp_path/'campaign.json';path.write_text(json.dumps({'limit_role_calls':26,'attempts':[]}))
 assert qa.reserve_campaign_call(path,'L1','invocation-1','a'*64,'explainer','explainer.v3')
 assert json.loads(path.read_text())['role_calls']==1
 # A restart reloads this ledger; reserved/unknown calls remain charged.
 again=_qa_module();assert again.reserve_campaign_call(path,'L1','invocation-1','a'*64,'explainer','explainer.v3') is False
 with pytest.raises(ValueError):again.reserve_campaign_call(path,'L1','invocation-1','b'*64,'explainer','explainer.v3')
 for i in range(2,5):again.reserve_campaign_call(path,'L1',f'invocation-{i}','a'*64,'explainer','explainer.v3')
 with pytest.raises(RuntimeError,match='BUDGET'):again.reserve_campaign_call(path,'L1','invocation-5','a'*64,'explainer','explainer.v3')
 for i in range(22):
  ledger=json.loads(path.read_text());ledger['attempts'].append({'scenario_id':'E1','invocation_id':f'historical-{i}','status':'outcome_unknown'});path.write_text(json.dumps(ledger))
 with pytest.raises(RuntimeError,match='BUDGET'):again.reserve_campaign_call(path,'L2','invocation-next','a'*64,'explainer','explainer.v3')

def test_campaign_case_remainder_prevents_writer_without_a_reviewer_slot(tmp_path):
 from p_mc_optimizer.workflow.narrative_v3 import NarrativeOrchestrator
 qa=_qa_module();path=tmp_path/'campaign.json';path.write_text(json.dumps({'limit_role_calls':26,'attempts':[{'scenario_id':'L1','invocation_id':f'previous-{i}','status':'outcome_unknown'} for i in range(3)]}))
 assert qa.campaign_remaining(path,'L1')==1 and qa.campaign_remaining(path,'L2')==4
 calls=[];r=json.loads(FIXTURE.read_text())
 result=NarrativeOrchestrator(r,invoke=lambda *args:calls.append(args),persist=lambda _:None,budget=lambda:{'total_used':0,'by_role':{},'max_total':qa.campaign_remaining(path,'L1'),'max_per_role':2},prompt_bundle_sha256='a'*64).run()
 assert calls==[] and result['mode']=='deterministic'

def test_completed_report_reopens_with_exhausted_campaign_without_provider_send(tmp_path,monkeypatch):
 from p_mc_optimizer.llm.credentials import CredentialBroker
 import httpx
 qa=_qa_module();qa.run_report_only(FIXTURE,tmp_path/'report',mode='offline')
 path=tmp_path/'campaign.json';path.write_text(json.dumps({'limit_role_calls':26,'attempts':[{'scenario_id':'E1','invocation_id':f'previous-{i}','status':'outcome_unknown'} for i in range(26)]}))
 # Controlled key resolution only: the historical draft/review above are mocks.
 # Any attempted network send fails this test; this is not a live quality test.
 monkeypatch.setattr(CredentialBroker,'get',lambda *args:'OFFLINE-NARRATIVE-FIXTURE-KEY')
 def forbidden(*args,**kwargs):raise AssertionError('completed report must not send a provider request')
 monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
 result=qa.run_report_only(FIXTURE,tmp_path/'report',mode='live',campaign_ledger=path,scenario='L1')
 assert result['campaign_remaining_at_start']==0 and result['role_calls']==2
 assert result['status']=='PASS' and result['counters']=={'objective_calls':0,'execution_calls':0}
 assert len(json.loads(path.read_text())['attempts'])==26

def test_actual_report_only_harness_runs_and_restarts_without_solver_or_external_calls(tmp_path):
 qa=_qa_module();first=qa.run_report_only(FIXTURE,tmp_path,mode='offline');second=qa.run_report_only(FIXTURE,tmp_path,mode='offline')
 assert first['status']==second['status']=='PASS'
 assert first['role_calls']==second['role_calls']==2
 assert first['external_role_calls']==second['external_role_calls']==0
 assert first['objective_evaluations']==second['objective_evaluations']==0
 assert second['counters']=={'objective_calls':0,'execution_calls':0}
 assert first['source_core_before']==second['source_core_after'] and second['numerical_core_unchanged']
 assert (tmp_path/'report.html').read_bytes()==(tmp_path/'reopened.html').read_bytes()
 generated=json.loads((tmp_path/'report.json').read_text());original=json.loads(FIXTURE.read_text())
 assert generated['source_run_llm_calls']==original['llm_calls']
 assert len(generated['llm_calls'])==2 and all(c['prompt_version']=='3.0' or c.get('provenance',{}).get('schema_id') in {'explainer.v3','narrative_review.v1'} for c in generated['llm_calls'])
 assert generated['qa_reporting']['ledger_scope']=='fresh_isolated_qa'
 assert generated['source_run_timing']==original['timing']
