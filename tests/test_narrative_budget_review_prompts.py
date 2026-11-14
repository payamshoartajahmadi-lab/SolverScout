"""Wire/context regressions only: no test here claims live semantic ability."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import httpx
import pytest

from p_mc_optimizer.llm.models import ProviderSnapshot
from p_mc_optimizer.llm.provider import prepare_provider_request, prompt_bundle_manifest
from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3, canonical_bytes, core_hash
from p_mc_optimizer.reporting.narrative_validation import validate_draft
from p_mc_optimizer.workflow.narrative_v3 import build_writer_context, build_reviewer_context
from test_narrative_v3_workflow import correct_draft


def paragraph(identifier, text, refs):
    return {'paragraph_id':identifier,'claim_kind':'recorded','text_fa':text,
            'fact_refs':refs,'evidence_refs':['report:proposal']}


def bound_counterexample(evidence, prompt_hash):
    writer=build_writer_context(evidence,prompt_bundle_sha256=prompt_hash)
    draft=correct_draft(writer.model_dump(mode='json'))
    sections={s['section_id']:s for s in draft['sections']}
    sections['candidate_rationale']['paragraphs']=[
        paragraph('candidate_de',
            'نامزدهای {{fact:proposal.methods}} از مسیر {{fact:proposal.source}} مطرح شدند. '
            'برای {{fact:method.m1.label}} تناسب با دامنهٔ پیوسته و کراندار ثبت شده است؛ '
            'این دلیل، توجیه نامزدی است و ادعای برتری عددی یا اثبات نتیجه نیست.',
            ['proposal.methods','proposal.source','method.m1.label']),
        paragraph('candidate_gwo',
            'برای {{fact:method.m2.label}} سازگاری با متغیرهای حقیقی کراندار ثبت شده است؛ '
            'این معرفی به‌تنهایی شاهد برتری در اجرا یا تأیید مستقل تنظیمات نیست.',
            ['method.m2.label']),
        paragraph('candidate_woa',
            'برای {{fact:method.m3.label}} تناسب با بهینه‌سازی تک‌هدفه ثبت شده است؛ '
            'این مبنای معرفی نامزد است، نه شاهدی بر عملکرد عددی بهتر.',
            ['method.m3.label']),
    ]
    sections['verification_and_limits']['paragraphs'].append({
        'paragraph_id':'verification_budget','claim_kind':'recorded',
        'text_fa':'مصرف اجرای اصلی {{fact:budget.main_started}} و پایلوت‌ها {{fact:budget.pilot_started}} بود؛ '
                  'مصرف کل ثبت‌شده برای اجرای اصلی و پایلوت‌ها {{fact:budget.total_started}} بود.',
        'fact_refs':['budget.main_started','budget.pilot_started','budget.total_started'],
        'evidence_refs':['report:budget','report:trials'],
    })
    checked=validate_draft(writer,draft)
    assert not checked.issues and not checked.global_error
    assert len(checked.valid_sections)==5
    return sections


@pytest.mark.parametrize('mode',['json_schema','json_object','text_json'])
def test_actual_provider_preparation_preserves_all_warning_paragraphs_and_budget_scope(mode,monkeypatch):
    """Prove full relevant input reaches both prompts, not a reviewer verdict."""
    def no_network(*args,**kwargs):
        raise AssertionError('Preparing a prompt must not send a provider request')
    monkeypatch.setattr(httpx,'Client',no_network)
    report=json.loads((Path(__file__).parent/'fixtures/narrative_v3/F4.json').read_text())
    before=core_hash(report);evidence=build_evidence_v3(report,policy_version='1.1')
    prompt_hash=sha256(canonical_bytes(prompt_bundle_manifest())).hexdigest()
    sections=bound_counterexample(evidence,prompt_hash);original=deepcopy(sections)
    scope=['candidate_rationale','verification_and_limits']
    reviewer=build_reviewer_context(evidence,list(sections.values()),prompt_bundle_sha256=prompt_hash,scope=scope)
    issues=[{'section_id':'candidate_rationale','paragraph_id':'candidate_gwo','code':'REDUNDANT_CONTENT',
             'evidence_refs':['report:proposal'],'message_fa':'هشدار مشترک میان پاراگراف‌ها تکرار شده است.',
             'required_change_fa':'دلایل واقعی را نگه دارید و محدودیت مشترک را یک بار بیان کنید.'},
            {'section_id':'verification_and_limits','paragraph_id':'verification_budget','code':'WRONG_BUDGET_SCOPE',
             'evidence_refs':['report:budget','report:trials'],'message_fa':'دامنهٔ عدد کل به دو مرحله محدود شده است.',
             'required_change_fa':'دامنهٔ کل کار را با همان fact معتبر حفظ کنید.'}]
    repair=build_writer_context(evidence,prompt_bundle_sha256=prompt_hash,scope=scope,
        accepted_sections=[s for k,s in sections.items() if k not in scope],
        failed_sections=[sections[k] for k in scope],repair_issues=issues)
    profile=ProviderSnapshot.model_validate({
        'preset':'custom_openai_compatible','base_url':'https://provider.example.test/v1','model':'offline-wire-fixture',
        'protocol':'chat_completions_v1','response_mode':mode,'token_limit_field':'max_completion_tokens',
        'max_output_tokens':2048,'connection_test_max_output_tokens':256,'request_timeout_seconds':5,
        'profile_id':'offline-wire-profile','revision':1,'credential_ref':'offline-unused-reference',
        'provider_snapshot_hash':'a'*64,
    })
    frozen=profile.model_dump(mode='json')
    for schema,context,prompt_name,cap in [('explainer.v3',repair,'explainer.v3.1',8192),
                                         ('narrative_review.v1',reviewer,'narrative_review.v1.1',4096)]:
        prepared=prepare_provider_request(profile,schema,context,report_generation_policy_version='1.1')
        body=json.loads(prepared.body_bytes);actual=json.loads(body['messages'][1]['content'])
        assert actual==context.model_dump(mode='json')
        assert prepared.provenance['prompt_name']==prompt_name
        assert prepared.provenance['request_body_sha256']==sha256(prepared.body_bytes).hexdigest()
        assert prepared.provenance['system_message_sha256']==sha256(body['messages'][0]['content'].encode()).hexdigest()
        expected=(Path(__file__).parents[1]/'src/p_mc_optimizer/llm/prompts'/(prompt_name+'.txt')).read_text().strip()
        assert body['messages'][0]['content']==expected if mode=='json_schema' else body['messages'][0]['content'].startswith(expected+'\nJSON Schema:')
        assert body['max_completion_tokens']==cap
        assert actual['evidence']==evidence.model_dump(mode='json')
        key='failed_sections' if schema=='explainer.v3' else 'draft_sections'
        candidate=next(s for s in actual[key] if s['section_id']=='candidate_rationale')
        assert candidate['paragraphs']==original['candidate_rationale']['paragraphs']
        budget=next(s for s in actual[key] if s['section_id']=='verification_and_limits')
        assert budget['paragraphs']==original['verification_and_limits']['paragraphs']
    assert profile.model_dump(mode='json')==frozen
    assert sections==original and core_hash(report)==before
