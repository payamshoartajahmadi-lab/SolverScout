"""Policy-1.1 transitions, frozen repair and healthy adversarial controls."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3,core_hash,value_hash
from p_mc_optimizer.reporting.narrative_validation import render_sections,validate_draft
from p_mc_optimizer.workflow.narrative_v3 import NarrativeOrchestrator,build_writer_context
from test_narrative_v3_workflow import Driver,fixture,correct_draft,review_response,BUNDLE


class ClosureDriver(Driver):
    def run(self,version='1.1',bundle=BUNDLE):
        return NarrativeOrchestrator(self.report,invoke=self.invoke,persist=self.save,journal=self.journal,
            budget=lambda:{'total_used':sum(self.used.values()),'by_role':self.used,'max_total':self.total,'max_per_role':2},
            current_report=lambda:self.report,cancelled=lambda:self.cancelled,shutdown=lambda:self.shutdown,
            prompt_bundle_sha256=bundle,report_generation_policy_version=version).run()


def test_new_explicit_policy_and_legacy_default_keep_core_and_call_limits():
    legacy=Driver();modern=ClosureDriver()
    before=core_hash(modern.report);result=modern.run()
    assert result['mode']=='llm' and result['evidence_builder_version']==result['formatter_version']=='1.1'
    assert result['evidence']['evidence_policy_version']=='1.1'
    assert 'proposal.rationale' not in {f['fact_id'] for f in result['evidence']['facts']}
    assert modern.journal['report_generation_policy_version']=='1.1'
    assert modern.journal['report_generation_policy']['roles']['explainer.v3']['max_output_tokens']==8192
    assert modern.used=={'explainer':1,'report_reviewer':1} and core_hash(modern.report)==before
    old=legacy.run();assert old['evidence_builder_version']==old['formatter_version']=='1.0'


def test_exact_binding_repair_retains_accepted_sections_and_evidence():
    driver=ClosureDriver()
    def writer(ctx):
        raw=correct_draft(ctx)
        if ctx['mode']=='draft':
            selection=next(x for x in raw['sections'] if x['section_id']=='selection')
            selection['paragraphs'][0]['text_fa']=selection['paragraphs'][0]['text_fa'].replace('{{fact:selection.decisive_criterion}}','معیار ثبت‌شده')
        return raw
    driver.writer=writer
    result=driver.run()
    assert result['mode']=='llm' and len(driver.sent)==4
    repair=driver.sent[2]['context']
    assert repair['section_scope']==['selection']
    diagnostics=' '.join(i['message_fa']+' '+i['required_change_fa'] for i in repair['repair_issues'])
    assert 'selection.decisive_criterion' in diagnostics and 'fact_refs_not_used_as_tokens' in diagnostics
    assert 'missing_required_fact_tokens' in diagnostics
    final={x['section']['section_id']:x['section'] for x in result['accepted_sections']}
    for accepted in repair['accepted_sections']:
        assert value_hash(accepted)==value_hash(final[accepted['section_id']])
    assert all(x['context']['evidence']==driver.sent[0]['context']['evidence'] for x in driver.sent)


@pytest.mark.parametrize('second_truncated',[False,True])
def test_truncated_writer_has_only_one_repair_and_never_publishes_unreviewed_text(second_truncated):
    driver=ClosureDriver()
    driver.writer=lambda ctx:({'status':'failed','call_id':'truncated-fixture','output':None,'previous_output':'{"sections":[',
                              'error':{'error_code':'LLM_OUTPUT_TRUNCATED'}} if ctx['mode']=='draft' or second_truncated else correct_draft(ctx))
    result=driver.run()
    assert driver.used['explainer']==2
    assert driver.used['report_reviewer']==(0 if second_truncated else 1)
    assert result['mode']==('deterministic' if second_truncated else 'llm')
    if second_truncated:assert all(x['origin']=='server' for x in result['accepted_sections'])


@pytest.mark.parametrize('change',['policy','prompt','policy_hash'])
def test_interrupted_journal_cannot_upgrade_policy_prompt_or_corrupted_definition(change):
    driver=ClosureDriver();driver.crash_after_send=True
    with pytest.raises(RuntimeError):driver.run()
    charged=sum(driver.used.values());old_version=driver.journal['report_generation_policy_version']
    if change=='policy':result=driver.run(version='1.0')
    elif change=='prompt':result=driver.run(bundle='b'*64)
    else:
        driver.journal['report_generation_policy_hash']='0'*64
        result=driver.run()
    assert sum(driver.used.values())==charged and result['mode']=='deterministic'
    assert driver.journal['report_generation_policy_version']==old_version
    assert any(x['code']=='CONTEXT_MISMATCH' for x in result['issues'])


def test_restart_same_journal_reuses_exact_invocation_and_request():
    driver=ClosureDriver();driver.crash_after_send=True
    with pytest.raises(RuntimeError):driver.run()
    first=deepcopy(driver.sent[0]);result=driver.run()
    assert result['mode']=='llm' and driver.used=={'explainer':1,'report_reviewer':1}
    assert driver.sent[0]==first and len(driver.sent)==2
    assert driver.run()==result and len(driver.sent)==2


def test_server_policy_change_during_writer_discards_old_prose_without_next_call():
    driver=ClosureDriver()
    def writer(ctx):
        raw=correct_draft(ctx);driver.report['report_generation_policy_version']='1.0';return raw
    driver.writer=writer;result=driver.run()
    assert len(driver.sent)==1 and result['mode']=='deterministic'
    assert all(x['origin']=='server' for x in result['accepted_sections'])
    assert any(x['code']=='CONTEXT_MISMATCH' for x in result['issues'])


@pytest.mark.parametrize('case',['C-A1','C-A2'])
def test_fresh_adversarial_cases_have_real_scoped_positive_controls_and_healthy_render(case):
    path=Path(__file__).parent/'fixtures/narrative_closure/cases.py'
    spec=importlib.util.spec_from_file_location('closure_fixture_cases',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    evidence=build_evidence_v3(fixture(),policy_version='1.1')
    raw,review,accept,reject,codes=module.make_case(evidence,case,BUNDLE)
    checked=validate_draft(build_writer_context(evidence,prompt_bundle_sha256=BUNDLE),raw)
    assert not checked.issues and not checked.global_error
    assert accept and set(accept)<=set(review.section_scope) and review.accepted_section_hashes==[]
    assert set(review.section_scope)==set(accept+reject) and set(codes)==set(reject)
    assert {x['section_id'] for x in raw['sections']} >= {'problem','candidate_rationale','selection','solution','verification_and_limits'}
    texts={s.section_id:' '.join(p.text_fa for p in s.paragraphs) for s in render_sections(evidence,raw['sections'])}
    assert all('است است' not in t and 'درصد درصد' not in t and '.؛' not in t for t in texts.values())
    if case=='C-A1':
        assert accept==['problem'] and 'زمان اجرا و مصرف حافظه' in texts['candidate_rationale']
        assert any(p['paragraph_id']=='adversarial_candidate_performance' for s in raw['sections'] for p in s['paragraphs'])
    else:assert accept==['mathematical_analysis'] and 'تضمین نمی‌شود' in texts['mathematical_analysis']
