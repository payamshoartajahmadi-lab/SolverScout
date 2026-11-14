"""Persistent campaign controls: no provider calls, no numerical execution."""
from copy import deepcopy
from contextlib import contextmanager
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from uuid import uuid4
import csv

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


def write_matrix(m, root, *, failed=None, evidence_path='run_manifest.json'):
    rows=[{'requirement_id':f'FC-{i:02d}','status':'FAIL' if i==failed else 'PASS',
           'reason_fa':'Observed evidence','evidence_paths':json.dumps([evidence_path]),'tested_source_hash':'b'*64} for i in range(1,35)]
    with (root/'acceptance_matrix.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    m.dump(root/'summary.json',{'scenarios':[{'scenario_id':s,'status':'NOT_RUN'} for s in m.ORDER],
        'actual':{'charged_role_attempts':0,'external_dispatches':0},'candidate_acceptance_status':'PASS',
        'narrative_closure_status':'PASS','delivery_preparation_readiness':'READY',
        'packaging_validation':'NOT_RUN','professor_machine_installation':'NOT_RUN'})


def test_verify_recomputes_gate_aggregation_instead_of_trusting_success_labels(tmp_path):
    m=prepared(tmp_path);write_matrix(m,tmp_path,failed=21)
    result=m.verify(tmp_path)
    assert result['status']=='FAIL'
    assert {'CANDIDATE_AGGREGATE_MISMATCH','CLOSURE_AGGREGATE_MISMATCH','READINESS_AGGREGATE_MISMATCH'}<=set(result['errors'])


def test_unexecuted_live_scenarios_cannot_be_promoted_by_all_pass_matrix(tmp_path):
    m=prepared(tmp_path);write_matrix(m,tmp_path)
    result=m.verify(tmp_path)
    assert result['status']=='FAIL'
    assert {f'LIVE_GATE_RESULT_MISMATCH:FC-{i:02d}' for i in range(21,28)}<=set(result['errors'])
    assert 'MISSING_FROZEN_SOURCE_FOR_ACCEPTANCE' in result['errors']


@pytest.mark.parametrize('evidence_path',['/etc/hosts','../outside.json'])
def test_verify_rejects_absolute_and_parent_traversing_evidence(tmp_path,evidence_path):
    m=prepared(tmp_path);write_matrix(m,tmp_path,evidence_path=evidence_path)
    assert 'UNSAFE_EVIDENCE_PATH:FC-01' in m.verify(tmp_path)['errors']


def test_verify_rejects_symlink_escape_in_relative_evidence(tmp_path):
    m=prepared(tmp_path);outside=tmp_path.parent/'outside-evidence.json';outside.write_text('{}')
    (tmp_path/'escape.json').symlink_to(outside);write_matrix(m,tmp_path,evidence_path='escape.json')
    assert 'UNSAFE_EVIDENCE_PATH:FC-01' in m.verify(tmp_path)['errors']


def test_unknown_case_type_cannot_bypass_pass_artifact_validation(tmp_path):
    m=prepared(tmp_path)
    m.dump(tmp_path/'live/C-L1/summary.json',{'scenario_id':'C-L1','case_type':'unrecognized','status':'PASS'})
    assert 'INVALID_SCENARIO_CASE_TYPE:C-L1' in m.verify(tmp_path)['errors']


def test_overall_summary_must_keep_the_defined_scenario_case_types(tmp_path):
    m=prepared(tmp_path);write_matrix(m,tmp_path)
    summary=m.read(tmp_path/'summary.json')
    for row in summary['scenarios']:
        row['case_type']='report_only' if row['scenario_id'] in m.REPORT_CASES else 'reviewer_adversarial'
    summary['scenarios'][0]['case_type']='report_only';m.dump(tmp_path/'summary.json',summary)
    assert 'INVALID_OVERALL_CASE_TYPE:C-A2' in m.verify(tmp_path)['errors']


def freeze_fixture(m, root):
    m.dump(root/'inputs/E1.json',{'objective':0})
    m.dump(root/'fixtures_manifest.json',m.input_manifest(root))
    junit=root/'offline/full-final.xml';junit.parent.mkdir();junit.write_text('<testsuite/>')
    frozen={'runtime':m.runtime_manifest(),'harness':m.harness_manifest(),
        'prompt_bundle_sha256':m.value_hash(m.prompt_bundle_manifest()),'prepared_cases':{},
        'fixtures_manifest_sha256':m.file_hash(root/'fixtures_manifest.json'),
        'full_regression_junit_sha256':m.file_hash(junit)}
    m.dump(root/'freeze.json',frozen)


def test_coordinated_input_and_manifest_rewrite_cannot_bypass_freeze(tmp_path):
    m=prepared(tmp_path);freeze_fixture(m,tmp_path);m.validate_freeze(tmp_path)
    m.dump(tmp_path/'inputs/E1.json',{'objective':999})
    m.dump(tmp_path/'fixtures_manifest.json',m.input_manifest(tmp_path))
    with pytest.raises(RuntimeError,match='FROZEN_FIXTURE_MANIFEST_CHANGED'):m.validate_freeze(tmp_path)


def test_rewritten_regression_evidence_is_not_accepted_after_freeze(tmp_path):
    m=prepared(tmp_path);freeze_fixture(m,tmp_path);m.validate_freeze(tmp_path)
    (tmp_path/'offline/full-final.xml').write_text('<testsuite tests="999"/>')
    with pytest.raises(RuntimeError,match='FROZEN_REGRESSION_EVIDENCE_CHANGED'):m.validate_freeze(tmp_path)
