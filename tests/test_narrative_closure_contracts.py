"""Closure contracts preserve free prose, exact diagnostics and immutable repair scopes."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from p_mc_optimizer.llm.models import provider_schema
from p_mc_optimizer.llm.narrative_models import NarrativeDraftV3,NarrativeReviewV1,WriterContextV3,NarrativeSectionV3,SECTION_ORDER
from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3,evidence_hash
from p_mc_optimizer.reporting.narrative_validation import validate_draft,validate_review,request_hash,section_hash,render_sections,NarrativeValidationError
from p_mc_optimizer.workflow.narrative_v3 import build_reviewer_context

ROOT=Path(__file__).parents[1]
def context(version='1.1'):
 r=json.loads((ROOT/'tests/fixtures/narrative_v3/F1.json').read_text());e=build_evidence_v3(r,policy_version=version)
 raw={'schema_version':'3.0','mode':'draft','prompt_bundle_sha256':'1'*64,'evidence':e.model_dump(mode='json'),'context_hash':evidence_hash(e),'request_hash':'0'*64,'section_scope':list(SECTION_ORDER),'accepted_sections':[],'failed_sections':[],'previous_response_text':None,'previous_response_truncated':False,'repair_issues':[]};raw['request_hash']=request_hash(raw);return WriterContextV3.model_validate(raw)
def draft(c,variant=0):
 sections=[]
 for s in c.evidence.sections:
  if not s.required:continue
  text='داده‌های ثبت‌شدهٔ این اجرا: '
  if s.section_id=='candidate_rationale':
   text=['ساختار پیوسته و کراندار مسئله مبنای مطرح‌شدن این نامزدهاست؛ دلیل مشترک را می‌توان گروهی توضیح داد و این پیشنهاد با نتیجهٔ پایلوت فرق دارد: ','نامزدها برای مسئلهٔ حقیقی و دارای کران مطرح شده‌اند. این توجیهِ پیشنهاد است و نتیجهٔ مقایسهٔ عددی را پیش‌بینی نمی‌کند: '][variant]
  text+='؛ '.join('{{fact:'+f+'}}' for f in s.required_fact_refs)+'.'
  ref={'problem':'report:problem','candidate_rationale':'report:proposal','selection':'report:decision','solution':'report:solution','verification_and_limits':'report:verification'}[s.section_id]
  sections.append({'section_id':s.section_id,'paragraphs':[{'paragraph_id':s.section_id,'claim_kind':'recorded','text_fa':text,'fact_refs':list(s.required_fact_refs),'evidence_refs':[ref]}]})
 return {'schema_version':'3.0','context_hash':c.context_hash,'request_hash':c.request_hash,'sections':sections}
def details(issue):return json.loads(issue.message_fa[issue.message_fa.index('{'):])
def selection(d):return next(s['paragraphs'][0] for s in d['sections'] if s['section_id']=='selection')

@pytest.mark.parametrize('variant',[0,1])
def test_independent_rationale_paraphrases_pass_without_aggregate_fact(variant):
 c=context();d=draft(c,variant);before=deepcopy(d);result=validate_draft(c,d)
 assert not result.issues and len(result.valid_sections)==5 and d==before
 assert 'proposal.rationale' not in {f.fact_id for f in c.evidence.facts}
 assert 'proposal.rationale' not in next(s for s in d['sections'] if s['section_id']=='candidate_rationale')['paragraphs'][0]['fact_refs']

def test_valid_but_irrelevant_proposal_reference_and_foreign_source_are_rejected():
 c=context();d=draft(c);p=next(s for s in d['sections'] if s['section_id']=='candidate_rationale')['paragraphs'][0]
 for refs in [['report:problem'],['source:another-job'],['unprefixed-candidate-internal-ref']]:
  p['evidence_refs']=refs;result=validate_draft(c,d)
  assert any(i.code=='UNKNOWN_EVIDENCE' and i.section_id=='candidate_rationale' for i in result.issues)
  assert len(result.valid_sections)==4
 d['context_hash']='f'*64;assert validate_draft(c,d).global_error

@pytest.mark.parametrize('fault',['token_without_ref','ref_without_token','duplicate_ref'])
def test_exact_binding_diff_diagnostics_never_auto_edit_model_text(fault):
 c=context();d=draft(c);p=selection(d);key='selection.decisive_criterion'
 if fault=='token_without_ref':p['fact_refs'].remove(key)
 elif fault=='ref_without_token':p['text_fa']=p['text_fa'].replace('{{fact:'+key+'}}','معیار انتخاب')
 else:p['fact_refs'].append(key)
 before=deepcopy(d);result=validate_draft(c,d);binding=next(i for i in result.issues if i.code=='INVALID_BINDING');diff=details(binding)
 assert diff=={'token_ids_not_in_fact_refs':[key] if fault=='token_without_ref' else [],'fact_refs_not_used_as_tokens':[key] if fault=='ref_without_token' else [],'duplicate_fact_refs':[key] if fault=='duplicate_ref' else []}
 assert d==before and len(result.valid_sections)==4
 assert 'بخش‌های پذیرفته‌شده را تغییر ندهید' in binding.required_change_fa
 if fault=='ref_without_token':
  missing=next(i for i in result.issues if i.code=='MISSING_REQUIRED_FACT')
  assert details(missing)=={'missing_required_fact_tokens':[key]}
  assert key in binding.message_fa and key in missing.message_fa and key in missing.required_change_fa

@pytest.mark.parametrize('text',['بودجهٔ اضافه ۲۰۰۰۰ ثبت شد.','استراتژی DE/rand/1/bin اجرا شد.'])
def test_unbound_number_reports_paragraph_and_short_unsafe_snippet_without_new_exceptions(text):
 c=context();d=draft(c);p=selection(d);p['text_fa']+=' '+text;before=deepcopy(d);r=validate_draft(c,d);i=next(i for i in r.issues if i.code=='UNBOUND_NUMBER');diagnostic=details(i)
 assert diagnostic['paragraph_id']=='selection' and len(diagnostic['unbound_number_snippet'])<=80
 assert any(ch.isdigit() for ch in diagnostic['unbound_number_snippet'])
 assert 'fact موجود' in i.required_change_fa and d==before

def test_repair_scope_hashes_and_frozen_evidence_remain_immutable():
 c=context();initial=validate_draft(c,draft(c));accepted=[s for s in initial.valid_sections if s.section_id!='selection'];raw=c.model_dump(mode='json');raw.update(mode='repair',section_scope=['selection'],accepted_sections=[s.model_dump(mode='json') for s in accepted]);raw['request_hash']=request_hash(raw);repair=WriterContextV3.model_validate(raw)
 output=draft(c);output.update(request_hash=repair.request_hash);output['sections']=[s for s in output['sections'] if s['section_id']=='selection'];before=deepcopy(output);hashes=[section_hash(s) for s in accepted];result=validate_draft(repair,output)
 assert not result.issues and hashes==[section_hash(s) for s in repair.accepted_sections] and evidence_hash(repair.evidence)==c.context_hash and output==before
 output['sections'].append(accepted[0].model_dump(mode='json'));output['sections'][-1]['paragraphs'][0]['text_fa']='بازنویسی خارج دامنه'
 assert validate_draft(repair,output).global_error and validate_draft(repair,output).issues[0].code=='OUT_OF_SCOPE_REPAIR'

@pytest.mark.parametrize('claim',['این روش‌ها همیشه سریع‌ترین و کم‌حافظه‌ترین روش‌ها هستند.','وجود این منبع برای هر مسئله‌ای برتری نامزدها را ثابت می‌کند.'])
def test_false_or_generic_claim_with_valid_refs_is_semantic_review_responsibility(claim):
 c=context();d=draft(c);p=next(s for s in d['sections'] if s['section_id']=='candidate_rationale')['paragraphs'][0];p['text_fa']+=' '+claim
 validated=validate_draft(c,d);assert not validated.issues
 rc=build_reviewer_context(c.evidence,validated.valid_sections,prompt_bundle_sha256=c.prompt_bundle_sha256,scope=['candidate_rationale'])
 response={'schema_version':'1.0','context_hash':rc.context_hash,'request_hash':rc.request_hash,'draft_hash':rc.draft_hash,'section_reviews':[{'section_id':'candidate_rationale','verdict':'reject','issues':[{'section_id':'candidate_rationale','paragraph_id':'candidate_rationale','code':'SOURCE_NOT_SUPPORTING','evidence_refs':['report:proposal'],'message_fa':'دلیل ثبت‌شده چنین برتری عمومی را پشتیبانی نمی‌کند.','required_change_fa':'ادعای ساختگی را حذف کنید و دلیل واقعی پیشنهاد را خلاصه کنید.'}]}]}
 assert validate_review(rc,response).section_reviews[0].verdict=='reject' # Controlled review proves software contracts, not real model understanding.

def test_legacy_frozen_display_text_is_used_without_new_formatter_or_builder():
 c=context('1.0');sections=validate_draft(c,draft(c)).valid_sections
 legacy=render_sections(c.evidence,sections);assert any('گواهی نشده است' in p.text_fa for s in legacy for p in s.paragraphs)
 assert 'درصد درصد' not in ' '.join(p.text_fa for s in legacy for p in s.paragraphs)
 assert render_sections(c.evidence.model_dump(mode='json'),[s.model_dump(mode='json') for s in sections])==legacy

def test_writer_and_reviewer_closed_response_schemas_are_unchanged():
 for name,model in [('explainer.v3',NarrativeDraftV3),('narrative_review.v1',NarrativeReviewV1)]:
  assert provider_schema(model)==json.loads((ROOT/'schemas'/(name+'.json')).read_text())
