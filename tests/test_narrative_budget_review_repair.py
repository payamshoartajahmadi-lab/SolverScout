"""Recorded L4 prose plus controlled reviewer decisions; not a live quality eval.

No fake in this module detects semantics. The prescribed decisions prove that
real orchestration transports Q5/Q6 errors into one bounded, scoped repair.
Corrected prose is an explicitly human-authored offline fixture, never a server
rewrite of the archived model output.
"""
from copy import deepcopy
import json
from pathlib import Path
import re

import httpx
import pytest

from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3, core_hash, value_hash
from p_mc_optimizer.reporting.narrative_validation import render_sections, validate_draft, validate_review
from p_mc_optimizer.workflow.narrative_v3 import build_writer_context, build_reviewer_context
from test_narrative_closure_workflow import ClosureDriver
from test_narrative_v3_workflow import BUNDLE, review_response


FIXTURES = Path(__file__).parent / 'fixtures'
RECORDED = FIXTURES / 'narrative_budget_review/actual_l4_failed_sections.json'
TOKEN = re.compile(r'\{\{fact:([^{}]+)\}\}')


@pytest.fixture(autouse=True)
def offline_report_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Offline Q5/Q6 contract tests cannot dispatch HTTP or numerical work')
    for target in ('httpx.HTTPTransport.handle_request',
                   'p_mc_optimizer.llm.gateway.LLMGateway.invoke',
                   'p_mc_optimizer.execution.evaluator.BudgetedEvaluator.__call__',
                   'p_mc_optimizer.execution.supervisor.run_execution'):
        monkeypatch.setattr(target, forbidden)


def report():
    return json.loads((FIXTURES / 'narrative_v3/F4.json').read_text())


def recorded_sections():
    return deepcopy(json.loads(RECORDED.read_text())['sections'])


def paragraph(identifier, text, refs):
    return {'paragraph_id':identifier, 'claim_kind':'recorded', 'text_fa':text,
            'fact_refs':list(dict.fromkeys(TOKEN.findall(text))), 'evidence_refs':refs}


def corrected_sections(*, variant=0):
    """Offline examples, preserving independent method reasons with one warning."""
    sections=recorded_sections();by_id={s['section_id']:s for s in sections}
    problem=by_id['problem']['paragraphs'][0]
    problem['text_fa']=problem['text_fa'].replace('هدف مسئله کمینه‌سازیِ', 'هدف مسئله {{fact:problem.sense}}ِ')
    if variant==0:
        # Three distinct reasons; the final sentence explicitly applies to all.
        tails={
            'candidate_de':'این دلیل، توجیه نامزدی است و ادعای برتری عددی یا اثبات نتیجه نیست.',
            'candidate_gwo':'این معرفی به‌تنهایی شاهد برتری در اجرا یا تأیید مستقل تنظیمات نیست.',
            'candidate_woa':'این مبنای معرفی نامزد است، نه شاهدی بر عملکرد عددی بهتر.',
        }
        for p in by_id['candidate_rationale']['paragraphs']:
            p['text_fa']=p['text_fa'].replace(tails[p['paragraph_id']], '').strip()
        by_id['candidate_rationale']['paragraphs'][-1]['text_fa']+=' این دلایل مشترکاً مبنای نامزدی‌اند و برتری عملکرد را ثابت نمی‌کنند.'
    else:
        # A different layout and wording, with two relevant distinct cautions.
        by_id['candidate_rationale']['paragraphs']=[
            paragraph('offline_candidates',
                'نامزدهای {{fact:proposal.methods}} از مسیر {{fact:proposal.source}} مطرح شدند. '
                'دلیل تکامل تفاضلی، تناسب خانواده با مسئلهٔ پیوسته و کراندار است؛ برای گرگ خاکستری، '
                'سازگاری با متغیرهای حقیقی کراندار و برای نهنگ، تناسب با بهینه‌سازی تک‌هدفهٔ کراندار ثبت شده است.',
                ['report:proposal']),
            paragraph('offline_shared_warning',
                'این دلیل‌ها توجیه بررسی نامزدها هستند؛ نتیجهٔ مقایسه را شواهد پایلوت تعیین می‌کند. '
                'کارت تکامل تفاضلی صرفاً تنظیم آزمایش‌شده را پوشش می‌دهد؛ مستند معرفی نهنگ نیز هویت روش را پشتیبانی می‌کند.',
                ['report:proposal']),
        ]
    budget=next(p for p in by_id['verification_and_limits']['paragraphs'] if p['paragraph_id']=='verification_budget')
    budget['text_fa']=(
        'مصرف اجرای اصلی {{fact:budget.main_started}} و مصرف پایلوت‌ها {{fact:budget.pilot_started}} ارزیابی آغازشده است. '
        'جمع این دو دامنه {{fact:budget.main_plus_pilot_started}} است، اما شمار کل کار {{fact:budget.total_started}} است. '
        'باقی‌ماندهٔ بیرون از این دو دامنه {{fact:budget.other_started}} است؛ از این تفاضل، نام مرحلهٔ مصرف‌کننده مشخص نمی‌شود. '
        'علت توقف ثبت‌شده {{fact:execution.termination_reason}} است.')
    budget['fact_refs']=list(dict.fromkeys(TOKEN.findall(budget['text_fa'])))
    budget['evidence_refs']=['report:budget','report:trials',
        'derived:budget.main_plus_pilot_started','derived:budget.other_started']
    return sections


def envelope(context, sections):
    return {'schema_version':'3.0','context_hash':context['context_hash'],
            'request_hash':context['request_hash'],'sections':deepcopy(sections)}


def prescribed_negative_review(context):
    """Deliberately injected test verdicts. This is not a model or a detector."""
    output=review_response(context)
    for section in output['section_reviews']:
        sid=section['section_id']
        if sid=='candidate_rationale':
            section.update(verdict='reject', issues=[{
                'section_id':sid, 'paragraph_id':pid, 'code':'REDUNDANT_CONTENT',
                'evidence_refs':['report:proposal'],
                'message_fa':'هشدار مشترک نبود شاهد برتری در پایان هر سه توضیح نامزد تکرار شده است؛ تفاوت واژگان، کارکرد یکسان آن‌ها را تغییر نمی‌دهد.',
                'required_change_fa':'دلیل مستقل هر روش را نگه دارید و این هشدار مشترک را فقط یک‌بار برای مجموعهٔ نامزدها بیان کنید.',
            } for pid in ('candidate_de','candidate_gwo','candidate_woa')])
        if sid=='verification_and_limits':
            section.update(verdict='reject', issues=[{
                'section_id':sid, 'paragraph_id':'verification_budget', 'code':'WRONG_BUDGET_SCOPE',
                'evidence_refs':['report:budget','report:trials','derived:budget.main_plus_pilot_started'],
                'message_fa':'عدد کل ۲۰۰۰۰ صحیح است اما دامنهٔ جمله غلط است؛ ۱۴۹۹۸ اصلی و ۴۵۰۰ پایلوت، جمعاً ۱۹۴۹۸ هستند، نه ۲۰۰۰۰.',
                'required_change_fa':'total_started را کل کار معرفی کنید؛ main و pilot و جمع مشتق معتبرشان را جدا بنویسید. باقی‌مانده را بدون شاهد به مرحله‌ای نسبت ندهید.',
            }])
    return output


def acceptance_control_sections(*, negative):
    """Fresh offline controls, with healthy non-target sections to avoid confounds.

    The two negative sections retain the actual failed L4 text. Healthy controls
    are human-authored examples, not an edited or repaired live response.
    """
    sections=recorded_sections() if negative else corrected_sections(variant=1)
    clean={
        'problem':paragraph('problem_main',
            'هدف، {{fact:problem.sense}} تابع {{fact:problem.expression}} با {{fact:problem.variable_count}} متغیر حقیقی است. '
            'دامنهٔ ثبت‌شده {{fact:problem.domain}} است.', ['report:problem']),
        'solution':paragraph('solution_point',
            'نقطهٔ تأییدشده {{fact:solution.x}} با مقدار خام {{fact:solution.objective_raw}} ثبت شده است. '
            'منشأ آن {{fact:solution.origin_phase}} است؛ نام روش تولیدکننده در رکورد منشأ {{fact:solution.origin_method_label}} است. '
            'روش منتخب پایلوت و منشأ این نقطه یک واقعیت واحد نیستند.', ['report:solution']),
    }
    for section in sections:
        if section['section_id'] in clean:section['paragraphs']=[deepcopy(clean[section['section_id']])]
    return sections


def test_recorded_failure_fixture_preserves_the_actual_raw_sections_and_missing_sense():
    raw=json.loads(RECORDED.read_text());sections=raw['sections']
    assert value_hash(sections)==raw['raw_sections_sha256']
    context=build_writer_context(build_evidence_v3(report(),policy_version='1.1'),prompt_bundle_sha256=BUNDLE)
    checked=validate_draft(context,envelope(context.model_dump(mode='json'),sections))
    assert not checked.global_error
    assert {s.section_id for s in checked.valid_sections}=={'candidate_rationale','selection','solution','verification_and_limits'}
    # Binding failures retain their ordinary raw section; invalid_sections is
    # specifically the bounded snapshot channel for structurally invalid JSON.
    assert {issue.section_id for issue in checked.issues}=={'problem'}
    assert any(i.code=='MISSING_REQUIRED_FACT' and 'problem.sense' in i.message_fa for i in checked.issues)
    # Binding validity does not prove the two semantically wrong sections correct.
    assert any(p['paragraph_id']=='verification_budget' and '{{fact:budget.total_started}}' in p['text_fa'] for s in sections for p in s['paragraphs'])


@pytest.mark.parametrize('variant',[0,1])
def test_different_offline_positive_examples_pass_binding_without_a_gold_prose_template(variant):
    evidence=build_evidence_v3(report(),policy_version='1.1')
    context=build_writer_context(evidence,prompt_bundle_sha256=BUNDLE)
    sections=corrected_sections(variant=variant)
    checked=validate_draft(context,envelope(context.model_dump(mode='json'),sections))
    assert not checked.global_error and not checked.issues and len(checked.valid_sections)==5
    reviewer=build_reviewer_context(evidence,sections,prompt_bundle_sha256=BUNDLE)
    accepted=validate_review(reviewer,review_response(reviewer.model_dump(mode='json')))
    assert all(s.verdict=='accept' for s in accepted.section_reviews)
    # This is the validity of a prescribed acceptance, not real reviewer ability.
    text=' '.join(p.text_fa for s in render_sections(evidence,sections) if s.section_id=='verification_and_limits' for p in s.paragraphs)
    assert all(str(n) in text for n in (14998,4500,19498,20000,502))


def test_actual_l4_two_semantic_rejections_and_sense_error_share_the_single_allowed_repair():
    source=report();before=deepcopy(source);driver=ClosureDriver(source,total=4)
    def writer(context):
        sections=recorded_sections() if context['mode']=='draft' else corrected_sections()
        sections=[s for s in sections if s['section_id'] in context['section_scope']]
        return envelope(context,sections)
    driver.writer=writer
    driver.reviewer=lambda ctx: prescribed_negative_review(ctx) if not ctx['accepted_section_hashes'] else review_response(ctx)
    result=driver.run()
    assert result['mode']=='llm' and result['review_status']=='passed'
    assert driver.used=={'explainer':2,'report_reviewer':2} and len(driver.sent)==4
    first_review=driver.sent[1]['context'];repair=driver.sent[2]['context'];last_review=driver.sent[3]['context']
    assert set(first_review['section_scope'])=={'candidate_rationale','selection','solution','verification_and_limits'}
    assert repair['section_scope']==last_review['section_scope']==['problem','candidate_rationale','verification_and_limits']
    assert {s['section_id'] for s in repair['failed_sections']}==set(repair['section_scope'])
    assert {s['section_id'] for s in repair['accepted_sections']}=={'selection','solution'}
    codes={(i['section_id'],i['code'],i['paragraph_id']) for i in repair['repair_issues']}
    assert ('verification_and_limits','WRONG_BUDGET_SCOPE','verification_budget') in codes
    assert all(('candidate_rationale','REDUNDANT_CONTENT',pid) in codes for pid in ('candidate_de','candidate_gwo','candidate_woa'))
    assert any(sid=='problem' and code=='MISSING_REQUIRED_FACT' for sid,code,_ in codes)
    original={s['section_id']:s for s in recorded_sections()}
    final={s['section']['section_id']:s['section'] for s in result['accepted_sections']}
    for sid in ('selection','solution'):
        assert value_hash(original[sid])==value_hash(final[sid])
        assert next(s['sha256'] for s in last_review['accepted_section_hashes'] if s['section_id']==sid)==value_hash(original[sid])
    assert all(call['context']['evidence']==driver.sent[0]['context']['evidence'] for call in driver.sent)
    assert core_hash(source)==core_hash(before) and source==before


def test_fresh_negative_control_is_fully_bound_and_prescribed_issues_target_actual_paragraphs():
    """The future live control isolates semantics from the old binding error."""
    evidence=build_evidence_v3(report(),policy_version='1.1')
    sections=acceptance_control_sections(negative=True)
    writer=build_writer_context(evidence,prompt_bundle_sha256=BUNDLE)
    checked=validate_draft(writer,envelope(writer.model_dump(mode='json'),sections))
    assert not checked.global_error and not checked.issues and len(checked.valid_sections)==5
    ctx=build_reviewer_context(evidence,sections,prompt_bundle_sha256=BUNDLE)
    output=prescribed_negative_review(ctx.model_dump(mode='json'))
    review=validate_review(ctx,output)
    assert {s.section_id for s in review.section_reviews if s.verdict=='accept'}=={'problem','selection','solution'}
    rejected={s.section_id:s for s in review.section_reviews if s.verdict=='reject'}
    assert set(rejected)=={'candidate_rationale','verification_and_limits'}
    assert [(i.code,i.paragraph_id) for i in rejected['verification_and_limits'].issues]==[('WRONG_BUDGET_SCOPE','verification_budget')]
    assert {i.paragraph_id for i in rejected['candidate_rationale'].issues}=={'candidate_de','candidate_gwo','candidate_woa'}
    assert {i.code for i in rejected['candidate_rationale'].issues}=={'REDUNDANT_CONTENT'}
    # Success here proves only the controlled review contract and real IDs.


@pytest.mark.parametrize('negative',[False,True])
def test_prepared_future_controls_have_full_valid_context_and_nonempty_scoped_positive_control(negative):
    evidence=build_evidence_v3(report(),policy_version='1.1')
    sections=acceptance_control_sections(negative=negative)
    writer=build_writer_context(evidence,prompt_bundle_sha256=BUNDLE)
    checked=validate_draft(writer,envelope(writer.model_dump(mode='json'),sections))
    assert not checked.issues and not checked.global_error and len(checked.valid_sections)==5
    reviewer=build_reviewer_context(evidence,sections,prompt_bundle_sha256=BUNDLE)
    positive={'problem','selection','solution'} if negative else {s['section_id'] for s in sections}
    assert positive and positive<=set(reviewer.section_scope) and not reviewer.accepted_section_hashes
    output=prescribed_negative_review(reviewer.model_dump(mode='json')) if negative else review_response(reviewer.model_dump(mode='json'))
    checked_review=validate_review(reviewer,output)
    assert {s.section_id for s in checked_review.section_reviews if s.verdict=='accept'}==positive


def test_repair_cannot_rewrite_the_two_accepted_sections_or_obtain_a_third_writer():
    driver=ClosureDriver(report(),total=4)
    def writer(ctx):
        if ctx['mode']=='draft':return envelope(ctx,recorded_sections())
        # An attacker returns all five despite a three-section scope.
        return envelope(ctx,corrected_sections())
    driver.writer=writer
    driver.reviewer=lambda ctx:prescribed_negative_review(ctx)
    result=driver.run()
    assert len(driver.sent)<=4 and driver.used['explainer']==2
    assert result['mode']!='llm'
    original={s['section_id']:s for s in recorded_sections()}
    final={s['section']['section_id']:s for s in result['accepted_sections']}
    for sid in ('selection','solution'):
        assert final[sid]['origin']=='llm' and value_hash(final[sid]['section'])==value_hash(original[sid])
