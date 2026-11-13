"""Persistent campaign controls: no provider calls, no numerical execution."""
from copy import deepcopy
from contextlib import contextmanager
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest
import httpx


def cli():
    name='closure_campaign_test_module';spec=importlib.util.spec_from_file_location(name,Path(__file__).parents[1]/'scripts/qa_narrative_v3_closure.py')
    result=importlib.util.module_from_spec(spec);sys.modules[name]=result;spec.loader.exec_module(result);return result


def prepared(tmp_path):
    m=cli();m.dump(tmp_path/'campaign_ledger.json',m.new_ledger('bounded-campaign'))
    m.dump(tmp_path/'run_manifest.json',{'campaign_id':'bounded-campaign'})
    m.write_usage(tmp_path);return m


def reserve(m,root,case,inv,role='report_reviewer'):
    return m.reserve_attempt(root,case,inv,'a'*64,role,'review','b'*64,'c'*64)


def test_reserved_and_unknown_charges_survive_restart_and_do_not_create_new_slots(tmp_path):
    m=prepared(tmp_path);assert reserve(m,tmp_path,'C-A2','id-one')
    m.update_attempt(tmp_path,'id-one',{'status':'outcome_unknown','dispatched_at':m.now()})
    restarted=cli();assert reserve(restarted,tmp_path,'C-A2','id-one') is False
    with pytest.raises(RuntimeError,match='BUDGET'):reserve(restarted,tmp_path,'C-A2','different-id')
    with pytest.raises(ValueError,match='CONFLICT'):
        restarted.reserve_attempt(tmp_path,'C-A2','id-one','d'*64,'report_reviewer','review','b'*64,'c'*64)
    assert len(m.read(tmp_path/'campaign_ledger.json')['attempts'])==1


def test_all_scenario_caps_and_global_cap_are_independent_and_conservative(tmp_path):
    m=prepared(tmp_path)
    for case in m.ORDER:
        for index in range(m.CAPS[case]):
            # Reviewer-only here isolates accounting, not writer/review orchestration.
            role='explainer' if case in m.REPORT_CASES and index in {0,2} else 'report_reviewer'
            m.reserve_attempt(tmp_path,case,case+str(index),'a'*64,role,'draft' if role=='explainer' else 'review','b'*64,'c'*64)
    assert len(m.read(tmp_path/'campaign_ledger.json')['attempts'])==22
    with pytest.raises(RuntimeError,match='BUDGET'):reserve(m,tmp_path,'C-E1-R','overflow')


@pytest.mark.parametrize('case,role',[('C-A1','explainer'),('A1','report_reviewer'),('C-L1','formulator')])
def test_unapproved_roles_and_renamed_scenarios_cannot_dispatch(tmp_path,case,role):
    m=prepared(tmp_path)
    with pytest.raises(ValueError,match='INVALID_CAMPAIGN_ROLE'):reserve(m,tmp_path,case,'attempt',role)
    assert m.read(tmp_path/'campaign_ledger.json')['attempts']==[]


def test_last_single_slot_cannot_start_writer_without_review(tmp_path):
    m=prepared(tmp_path)
    reserve(m,tmp_path,'C-L1','first','explainer');reserve(m,tmp_path,'C-L1','review-one')
    reserve(m,tmp_path,'C-L1','review-two')
    with pytest.raises(RuntimeError,match='REVIEW_CAPACITY'):reserve(m,tmp_path,'C-L1','writer-two','explainer')
    assert len(m.read(tmp_path/'campaign_ledger.json')['attempts'])==3


def test_first_definitive_fail_closes_all_later_live_scenarios(tmp_path,monkeypatch):
    m=prepared(tmp_path);m.dump(tmp_path/'inputs/F1.json',{})
    monkeypatch.setattr(m,'validate_freeze',lambda _: {'provider_profile':{},'tested_source_commit':'0'*40})
    monkeypatch.setattr(m,'source_provider_summary',lambda *a:{})
    calls=[]
    def fail(root,scenario,provider):
        calls.append(scenario)
        return {'scenario_id':scenario,'status':'FAIL','stage':'finished','reason_fa':'positive control rejected'}
    monkeypatch.setattr(m,'run_adversarial',fail)
    result=m.run_live(tmp_path,Path('/unused'))
    assert result['state']=='failed' and calls==['C-A2']
    assert m.run_live(tmp_path,Path('/unused'))['state']=='failed' and calls==['C-A2']
    assert m.read(tmp_path/'campaign_ledger.json')['stop_reason'].startswith('C-A2:')


def test_verify_zero_dispatch_prepared_ledger_is_read_only_and_not_feature_acceptance(tmp_path):
    m=prepared(tmp_path)
    before={str(p):sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob('*') if p.is_file()}
    result=m.verify(tmp_path)
    after={str(p):sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob('*') if p.is_file()}
    assert before==after and result['status']=='PASS' and result['charged_role_attempts']==result['external_dispatches']==0
    assert 'never upgrades feature acceptance' in result['verification_scope']


def test_verify_rejects_duplicate_or_unknown_required_result_sets(tmp_path):
    m=prepared(tmp_path)
    scenarios=[{'scenario_id':s,'status':'NOT_RUN'} for s in m.ORDER]
    scenarios[-1]['scenario_id']=scenarios[0]['scenario_id'];m.dump(tmp_path/'summary.json',{'scenarios':scenarios})
    result=m.verify(tmp_path);assert result['status']=='FAIL' and 'SUMMARY_SCENARIO_SET' in result['errors']


def test_public_numerical_authorization_survives_secret_redaction(tmp_path):
    m=prepared(tmp_path)
    source={'trials':[{'numeric_execution':{'authorization':{'public_contract':'value','nonce':'numeric'}},'objective_raw':0.0}],
            'llm_profile':{'base_url':'https://provider.invalid','credential_ref':'private-reference'},'headers':{'Authorization':'Bearer secret'}}
    m.dump(tmp_path/'report.json',source)
    output=m.read(tmp_path/'report.json')
    assert output['trials']==source['trials'] and output['llm_profile']=={} and 'headers' not in output


def test_numerical_guard_stops_before_actual_call_and_durably_closes_campaign(tmp_path):
    m=prepared(tmp_path)
    with m.no_numerics(tmp_path) as counters:
        from p_mc_optimizer.execution.evaluator import BudgetedEvaluator
        with pytest.raises(RuntimeError,match='NUMERICAL_CALL_FORBIDDEN'):BudgetedEvaluator.__call__(None,None)
    ledger=m.read(tmp_path/'campaign_ledger.json');assert ledger['state']=='failed'
    assert counters=={'objective_calls':1,'solver_calls':0} and ledger['objective_call_guard_violations']==1
    assert ledger['fresh_objective_evaluations']==0 # blocked before evaluating anything
    assert 'NUMERICAL_GUARD_VIOLATION' in m.verify(tmp_path)['errors']


def test_human_quality_review_requires_all_six_actual_paragraphs_and_hash(tmp_path):
    m=prepared(tmp_path);root=tmp_path/'live/C-L1'
    m.dump(root/'report.json',{'narrative_v3':{'rendered_sections':[{'paragraphs':[{'paragraph_id':'real'}]}]}})
    review={'report_sha256':m.file_hash(root/'report.json'),'rubric':[{'id':f'Q{i}','status':'PASS','paragraph_ids':['real'],'reason_fa':'Specific read'} for i in range(1,7)]}
    m.dump(root/'quality_review.json',review)
    summary={'stage':'awaiting_quality_review','report_sha256':review['report_sha256']}
    result=m.complete_human_review(tmp_path,'C-L1',deepcopy(summary));assert result['status']=='PASS'
    review['rubric'][-1]['paragraph_ids']=['invented'];m.dump(root/'quality_review.json',review)
    with pytest.raises(RuntimeError,match='UNSUPPORTED_QUALITY_CLAIM'):m.complete_human_review(tmp_path,'C-L1',deepcopy(summary))


def test_dispatch_hook_checks_actual_reserved_body_and_forbids_hidden_retry(tmp_path):
    m=prepared(tmp_path);reserve(m,tmp_path,'C-A2','real-invocation')
    body=m.canonical_bytes({'model':'unchanged','max_completion_tokens':4096})
    proof={'request_body_sha256':sha256(body).hexdigest(),'report_generation_policy_version':'1.1',
           'report_generation_policy_hash':m.freeze_narrative_policy('1.1')['report_generation_policy_hash']}
    class Connection:
        def execute(self,*args):return self
        def fetchone(self):return {'call_id':'durable-call','profile_revision':3,'metadata':json.dumps({'provenance':proof})}
    class Repo:
        @contextmanager
        def connect(self):yield Connection()
    transport=m.DispatchTransport(tmp_path,Repo());transport.inner.close()
    calls=[];transport.inner=httpx.MockTransport(lambda request:(calls.append(request) or httpx.Response(200,json={'ok':True})))
    transport.invocation='real-invocation'
    request=httpx.Request('POST','https://example.invalid/v1/chat/completions',content=body)
    response=transport.handle_request(request);assert response.status_code==200
    with pytest.raises(RuntimeError,match='HIDDEN_EXTERNAL_RETRY'):transport.handle_request(request)
    ledger=m.read(tmp_path/'campaign_ledger.json');assert ledger['external_dispatches']==1 and len(calls)==1
    assert ledger['state']=='failed' and ledger['stop_reason']=='HIDDEN_EXTERNAL_RETRY'
    assert ledger['attempts'][0]['request_body_sha256']==proof['request_body_sha256']
    assert ledger['attempts'][0]['effective_max_output_tokens']==4096
    assert ledger['attempts'][0]['call_id']=='durable-call'
    transport.finish()


def test_gateway_unknown_cannot_hide_deterministic_campaign_invariant_failure(tmp_path,monkeypatch):
    m=prepared(tmp_path);source=json.loads((Path(__file__).parent/'fixtures/narrative_v3/F1.json').read_text())
    m.dump(tmp_path/'fixtures_manifest.json',{'cases':{'C-A2':{'source_core_sha256':m.core_hash(source)}}})
    m.dump(tmp_path/'freeze.json',{'runtime':{'canonical_source_sha256':'b'*64}})
    monkeypatch.setattr(m,'validate_freeze',lambda _:None)
    class Repo:
        def get_job(self,*args):return {'workflow':{'narrative_v3_journal':{'active_request':{'operation':'review'}}},'llm_profile':{'revision':3}}
        def put_workflow(self,*args):pass
    class Gateway:
        def invoke(self,*args):
            m.close_campaign(tmp_path,'failed','EFFECTIVE_OUTPUT_CAP_MISMATCH')
            return {'status':'outcome_unknown','metadata':{},'error':{'error_code':'LLM_OUTCOME_UNKNOWN'},'call_id':None}
    class Transport:invocation=None
    callback=m.invocation_callback(tmp_path,'C-A2',Repo(),Gateway(),Transport(),source,'live')
    with pytest.raises(RuntimeError,match='EFFECTIVE_OUTPUT_CAP_MISMATCH'):
        callback('report_reviewer','narrative_review.v1',{'request_hash':'a'*64},str(uuid4()))
    assert m.read(tmp_path/'campaign_ledger.json')['state']=='failed'


def test_verify_rejects_invalid_status_even_with_exactly_seven_case_ids(tmp_path):
    m=prepared(tmp_path);scenarios=[{'scenario_id':s,'status':'NOT_RUN'} for s in m.ORDER]
    scenarios[0]['status']='BOGUS';m.dump(tmp_path/'summary.json',{'scenarios':scenarios,'actual':{'charged_role_attempts':0,'external_dispatches':0}})
    result=m.verify(tmp_path)
    assert result['status']=='FAIL' and 'INVALID_OVERALL_SCENARIO_STATUS' in result['errors']
