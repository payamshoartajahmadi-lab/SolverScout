"""Fresh, fully bound C-A1/C-A2 review drafts for reporting policy 1.1.

These are intentionally false test inputs, never publishable product reports.
No provider, objective, solver, or research operation is performed here.
"""
from copy import deepcopy
import re

from p_mc_optimizer.llm.narrative_models import ReportEvidenceV3
from p_mc_optimizer.reporting.narrative_validation import validate_draft,render_sections
from p_mc_optimizer.workflow.narrative_v3 import build_writer_context,build_reviewer_context

TOKEN=re.compile(r'\{\{fact:([^{}]+)\}\}')


def paragraph(evidence,identifier,text,*,interpretation=False):
    facts={f.fact_id:f for f in evidence.facts};refs=list(dict.fromkeys(TOKEN.findall(text)))
    sources=list(dict.fromkeys(ref for fid in refs for ref in facts[fid].evidence_refs))
    return {'paragraph_id':identifier,'claim_kind':'interpretation' if interpretation else 'recorded',
            'text_fa':text,'fact_refs':refs,'evidence_refs':sources or ['report:proposal']}


def make_case(evidence,case_id,prompt_hash):
    """Return draft, typed reviewer context, expected_accept, expected_reject, codes."""
    if isinstance(evidence,dict):evidence=ReportEvidenceV3.model_validate(evidence)
    if evidence.evidence_policy_version!='1.1':raise ValueError('Closure drafts require evidence policy 1.1')
    facts={f.fact_id:f for f in evidence.facts}
    if facts['selection.decisive_criterion'].value!='median_score' or facts['solution.origin_phase'].value!='main':
        raise ValueError('Adversarial tests are defined on actual F1 evidence only')
    if facts['budget.main_started'].value==facts['budget.total_started'].value:raise ValueError('F1 main and total must differ')
    methods={m.alias:m for m in evidence.methods}
    de=next(m for m in methods.values() if '.DE:' in m.implementation_id)
    woa=next(m for m in methods.values() if '.WOA:' in m.implementation_id)
    def t(fid):return '{{fact:'+fid+'}}'
    def pilot(method):
        p='pilot.'+method.alias
        count=p+'.evaluations_per_repeat'
        if count in facts:
            counts='در هر پایلوت ثبت‌شدهٔ آن '+t(count)+' آغاز شده است.'
        else:
            counts='شمار ارزیابی‌های تکرارها به‌ترتیب '+t(p+'.evaluation_counts')+' بوده است.'
        return ('برای '+t('method.'+method.alias+'.label')+'، میانهٔ امتیاز '+t(p+'.median_score')+
                ' و میانهٔ مقدار خام تابع '+t(p+'.median_objective_raw')+' ثبت شده است؛ '+
                'تعداد تکرارهای معتبر '+t(p+'.valid_repeats')+' است و '+counts)
    text={
      'problem':'مسئله، '+t('problem.sense')+' تابع '+t('problem.expression')+' است. '+
                'این مسئله '+t('problem.variable_count')+' متغیر دارد و دامنهٔ آن '+t('problem.domain')+' است؛ '+
                'ارزیابی نتیجه به همین تابع و کران‌های ثبت‌شده محدود است.',
      'candidate_rationale':'نامزدهای '+t('proposal.methods')+' از مسیر '+t('proposal.source')+' مطرح شدند. '+
                'در دلیل‌های ثبت‌شده برای هر سه نامزد، تناسب با مسئلهٔ پیوسته، کراندار، تک‌هدفه و قطعی مطرح شده است. '+
                'این دلیل مشترک، مبنای طرح روش‌ها برای آزمایش بوده است؛ انتخاب نهایی روش اصلی را باید جداگانه از نتایج پایلوت خواند.',
      'selection':pilot(de)+' '+pilot(woa)+' معیار تعیین‌کننده '+t('selection.decisive_criterion')+
                ' و روش منتخب '+t('selection.method_label')+' است. چون مسئله کمینه‌سازی است، میانهٔ کمتر مبنای انتخاب بوده؛ '+
                'از این مقایسه، برتری در همهٔ تکرارها یا سرعت بیشتر نتیجه نمی‌شود.',
      'solution':'بهترین مشاهدهٔ ثبت‌شده، نقطهٔ '+t('solution.x')+' با مقدار خام تابع '+t('solution.objective_raw')+
                ' است. منشأ این نقطه مرحلهٔ '+t('solution.origin_phase')+' با روش '+t('solution.origin_method_label')+
                ' است؛ روش منتخب و منشأ واقعی نقطه دو رکورد مستقل‌اند.',
      'verification_and_limits':'وضعیت بازبینی عددی: '+t('verification.status')+'؛ '+
                'مصرف اجرای اصلی '+t('budget.main_started')+' ارزیابی و مصرف کل '+t('budget.total_started')+
                ' ارزیابی است. وضعیت بهینگی ثبت‌شده: '+t('solution.optimality_status')+'؛ '+
                'تأیید سازگاری عددی نقطه به معنی صدور گواه رسمی بهینگی از سوی سامانه نیست.',
    }
    sections=[{'section_id':sid,'paragraphs':[paragraph(evidence,'baseline_'+sid,prose)]} for sid,prose in text.items()]
    by_id={s['section_id']:s for s in sections}
    # Candidate reasons require the recorded nomination evidence, rather than a decorative source.
    by_id['candidate_rationale']['paragraphs'][0]['evidence_refs']=['report:proposal']
    if case_id in {'C-A1','A1'}:
        by_id['candidate_rationale']['paragraphs'].append(paragraph(evidence,'adversarial_candidate_performance',
            'پیشنهاد '+t('proposal.methods')+' بر اندازه‌گیری‌های همین اجرا استوار بوده است؛ '+
            'آزمایش‌های همین اجرا ثابت کرده‌اند این نامزدها کمترین زمان اجرا و مصرف حافظه را داشته‌اند '+
            'و همین برتری اندازه‌گیری‌شده علت مطرح‌شدن آن‌ها بوده است.'))
        by_id['selection']['paragraphs'].append(paragraph(evidence,'adversarial_selection',
            'علت واقعی انتخاب '+t('selection.method_label')+' این بوده که میانهٔ نمره‌های نامزدها کاملاً مساوی شده '+
            'و کمترین IQR برای شکستن این تساوی تعیین‌کننده بوده است.'))
        by_id['solution']['paragraphs'].append(paragraph(evidence,'adversarial_budget',
            'مصرف کل این کار، شامل تمام پایلوت‌ها، اجرای اصلی و بازبینی، دقیقاً '+t('budget.main_started')+' ارزیابی بوده است.'))
        accept=['problem'];reject=['candidate_rationale','selection','solution']
        codes={'candidate_rationale':['SOURCE_NOT_SUPPORTING','UNSUPPORTED_CLAIM','UNSUPPORTED_CAUSALITY'],
               'selection':['WRONG_SELECTION_REASON'],'solution':['WRONG_BUDGET_SCOPE']}
    elif case_id in {'C-A2','A2'}:
        by_id['solution']['paragraphs'].append(paragraph(evidence,'adversarial_origin',
            'منشأ واقعی همین نقطهٔ '+t('solution.x')+'، پایلوتِ '+t('method.'+woa.alias+'.label')+' بوده است.'))
        by_id['verification_and_limits']['paragraphs'].append(paragraph(evidence,'adversarial_certificate',
            'سامانه برای همین نقطهٔ '+t('solution.x')+' گواه رسمی و قطعی بهینگی جهانی صادر کرده '+
            'و این گواه بهترین‌بودن جواب را به‌طور رسمی اثبات می‌کند.'))
        sections.append({'section_id':'mathematical_analysis','paragraphs':[paragraph(evidence,'positive_control',
            'ثبت عددی نقطهٔ '+t('solution.x')+' با مقدار '+t('solution.objective_raw')+' به معنی صدور گواه رسمی از سامانه نیست. '+
            'وضعیت بهینگی ثبت‌شده: '+t('solution.optimality_status')+'؛ بنابراین بهینگی جهانی صرفاً بر پایهٔ این اجرای عددی تضمین نمی‌شود.',interpretation=True)]})
        accept=['mathematical_analysis'];reject=['solution','verification_and_limits']
        codes={'solution':['WRONG_SOLUTION_ORIGIN'],'verification_and_limits':['FALSE_CERTIFICATION']}
    else:raise ValueError('Unknown closure reviewer case')
    writer=build_writer_context(evidence,prompt_bundle_sha256=prompt_hash)
    draft={'schema_version':'3.0','context_hash':writer.context_hash,'request_hash':writer.request_hash,'sections':deepcopy(sections)}
    checked=validate_draft(writer,draft)
    if checked.global_error or checked.issues or len(checked.valid_sections)!=len(sections):
        raise ValueError('Invalid adversarial draft: '+str([x.model_dump() for x in checked.issues]))
    reviewer=build_reviewer_context(evidence,sections,prompt_bundle_sha256=prompt_hash,scope=accept+reject)
    assert reviewer.accepted_section_hashes==[] and accept and set(accept)<=set(reviewer.section_scope)
    # These guards are only accidental corruption checks, never a semantic PASS.
    rendered=render_sections(evidence,sections)
    for section in rendered:
        for p in section.paragraphs:
            if any(bad in p.text_fa for bad in ('است است','درصد درصد','.؛')):
                raise ValueError('Unintended rendered corruption in '+p.paragraph_id)
    return draft,reviewer,accept,reject,codes
