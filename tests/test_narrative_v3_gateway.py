"""New version dispatch and single-attempt gateway over real loopback HTTP."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from p_mc_optimizer.llm.client import invoke_role
from p_mc_optimizer.llm.provider import prompt_bundle_manifest
from p_mc_optimizer.reporting.evidence_v3 import canonical_bytes, core_hash
from p_mc_optimizer.reporting.narrative_v3 import apply_narrative_v3
from p_mc_optimizer.workflow.narrative_v3 import NarrativeOrchestrator
from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, checked, golden
from test_llm_workflow import _GOOD_PLAN, configure, local_api, submit, work
from test_narrative_v3_workflow import correct_draft, review_response
from test_llm_gateway import completion


@pytest.mark.parametrize('mode', ['json_schema','json_object','text_json'])
def test_typed_narrative_gateway_modes_and_exact_journal_replay(tmp_path, mode):
    report=json.loads((Path(__file__).parent/'fixtures/narrative_v3/F1.json').read_text())
    sent=[]
    def provider(request):
        body=json.loads(request.content);text=body['messages'][1]['content']
        if text=='Return the connection check as JSON.': return completion({'ok':True})
        ctx=json.loads(text);sent.append(ctx)
        assert ctx['evidence']['job_id']==report['job_id']
        assert len(body['messages'][0]['content'])>100
        if mode!='json_schema':assert 'JSON Schema:' in body['messages'][0]['content']
        return completion(correct_draft(ctx) if 'mode' in ctx else review_response(ctx))
    with local_api(tmp_path,httpx.MockTransport(provider)) as (repo,client,address,token):
        profile,_=configure(client,{**_PROFILE_SETTINGS,'response_mode':mode},_TEST_KEY)
        job_id=submit(client,profile,golden()['original_text']);owner=str(uuid4());assert repo.claim_job(owner)['job_id']==job_id
        repo.update_job(job_id,phase='reporting',worker_id=owner)
        assert repo.get_job(job_id)['workflow']['execution_contracts']['explanation']=='grounded_narrative_v3'
        # Preserve this historical 1.0 gateway fixture explicitly; fresh 1.1
        # wire settings and policy conflicts have their own closure tests.
        contracts=deepcopy(repo.get_job(job_id)['workflow']['execution_contracts'])
        contracts.pop('report_generation_policy_version',None);contracts.pop('report_generation_policy',None)
        contracts.update(evidence_builder_version='1.0',formatter_version='1.0')
        repo.put_workflow(job_id,'execution_contracts',contracts,worker_id=owner)
        report=json.loads(json.dumps(report).replace(report['job_id'],job_id));original=core_hash(report);calls=[]
        def budget():
            with repo.connect() as db:rows=db.execute('SELECT role FROM llm_calls WHERE job_id=?',(job_id,)).fetchall()
            return {'total_used':len(rows),'by_role':{r:sum(x['role']==r for x in rows) for r in ['explainer','report_reviewer']},'max_total':6,'max_per_role':2}
        def invoke(role,schema,context,invocation):
            calls.append((role,schema,deepcopy(context),invocation))
            return invoke_role(address,token,job_id,role,context,invocation_id=invocation,schema_id=schema,request_timeout_seconds=5)
        engine=NarrativeOrchestrator(report,invoke=invoke,persist=lambda v:repo.put_workflow(job_id,'narrative_v3_journal',v,worker_id=owner),budget=budget,prompt_bundle_sha256=sha256(canonical_bytes(prompt_bundle_manifest())).hexdigest())
        result=engine.run();assert result['mode']=='llm',result
        assert result['review_status']=='passed';assert len(calls)==len(sent)==2
        assert [x[:2] for x in calls]==[('explainer','explainer.v3'),('report_reviewer','narrative_review.v1')]
        assert core_hash(apply_narrative_v3(report,result))==original
        role,schema,context,invocation=calls[-1]
        replay=invoke_role(address,token,job_id,role,context,invocation_id=invocation,schema_id=schema,request_timeout_seconds=5)
        assert replay['call_id'] and len(sent)==2 and budget()['total_used']==2
        bad=deepcopy(context);bad['section_scope']=['problem']
        denied=invoke_role(address,token,job_id,role,bad,invocation_id=str(uuid4()),schema_id=schema,request_timeout_seconds=5)
        assert denied['call_id'] is None and len(sent)==2


def test_new_full_workflow_uses_writer_reviewer_without_new_objective_calls(tmp_path):
    sent=[];at_reporting=[]
    def provider(request):
        body=json.loads(request.content);text=body['messages'][1]['content']
        if text=='Return the connection check as JSON.':return completion({'ok':True})
        ctx=json.loads(text)
        if 'sources' in ctx and 'evidence' not in ctx:return completion(golden()['response'])
        if 'candidate_count' in ctx:return completion(deepcopy(_GOOD_PLAN))
        assert 'evidence' in ctx
        at_reporting.append(repo.get_job(job_id)['budget']['known_started'])
        sent.append(ctx);return completion(correct_draft(ctx) if 'mode' in ctx else review_response(ctx))
    with local_api(tmp_path,httpx.MockTransport(provider)) as (repo,client,address,token):
        profile,_=configure(client,_PROFILE_SETTINGS,_TEST_KEY);job_id=submit(client,profile,golden()['original_text'])
        work(repo,address,token,job_id,legacy_explanation=None)
        state=checked(client.get('/jobs/'+job_id));report=checked(client.get('/jobs/'+job_id+'/report'))['report']
        assert state['job_status']=='completed'
        assert report['narrative_v3']['mode']=='llm' and report['explanation_contract_version']=='3.0'
        assert report['explanation_notes']==[]
        assert at_reporting==[1002,1002] and report['budget']['known_started']==1002
        assert [x['role'] for x in report['llm_calls']]==['formulator','advisor','explainer','report_reviewer']


def test_full_workflow_discards_draft_after_persisted_core_changes(tmp_path):
    """The post-draft guard must reread persistence, not its cached report."""
    reporting_requests=[]
    before={}
    changed_rationale='دلیل ثبت‌شده پس از تولید پیش‌نویس تغییر کرده است.'

    def provider(request):
        body=json.loads(request.content)
        text=body['messages'][1]['content']
        if text=='Return the connection check as JSON.':
            return completion({'ok':True})
        ctx=json.loads(text)
        if 'sources' in ctx and 'evidence' not in ctx:
            return completion(golden()['response'])
        if 'candidate_count' in ctx:
            return completion(deepcopy(_GOOD_PLAN))
        assert 'evidence' in ctx
        reporting_requests.append(ctx)
        assert ctx['mode']=='draft', 'No reviewer or repair may use the stale evidence.'
        before['core']=ctx['evidence']['source_core_sha256']
        before['budget']=deepcopy(repo.get_job(job_id)['budget'])
        before['executions']=deepcopy(repo.list_executions(job_id))
        plan=deepcopy(repo.get_job(job_id)['workflow']['candidate_plan'])
        plan['candidates'][0]['rationale_fa']=changed_rationale
        # Simulate a real persisted update arriving while the provider is in flight.
        # Numerical results, seeds, thresholds and budgets remain untouched.
        repo.put_workflow(job_id,'candidate_plan',plan)
        return completion(correct_draft(ctx))

    with local_api(tmp_path,httpx.MockTransport(provider)) as (repo,client,address,token):
        profile,_=configure(client,_PROFILE_SETTINGS,_TEST_KEY)
        job_id=submit(client,profile,golden()['original_text'])
        work(repo,address,token,job_id,legacy_explanation=None)
        state=checked(client.get('/jobs/'+job_id))
        report=checked(client.get('/jobs/'+job_id+'/report'))['report']
        narrative=report['narrative_v3']
        assert state['job_status']=='completed'
        assert report['candidate_plan']['candidates'][0]['rationale_fa']==changed_rationale
        assert len(reporting_requests)==1
        assert [x['role'] for x in report['llm_calls']]==['formulator','advisor','explainer']
        assert narrative['mode']=='deterministic'
        assert all(section['origin']=='server' for section in narrative['accepted_sections'])
        assert any(issue['code']=='CONTEXT_MISMATCH' for issue in narrative['issues'])
        assert narrative['source_core_sha256']==core_hash(report)!=before['core']
        assert report['budget']==before['budget']
        assert report['budget']['known_started']==1002
        assert repo.list_executions(job_id)==before['executions']
