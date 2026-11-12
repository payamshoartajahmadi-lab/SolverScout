#!/usr/bin/env python3
"""Explicit, isolated report-only acceptance for grounded narrative v3.

No objective, optimization, web research or connection warm-up is performed.
Private QA SQLite state is kept in _private_qa and MUST be excluded from bundles.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, UTC
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

import httpx
from filelock import FileLock

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT/'src') not in sys.path:sys.path.insert(0,str(ROOT/'src'))

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.llm.gateway import LLMGateway
from p_mc_optimizer.llm.models import CredentialMutationTarget, Invocation, ProfileUpdate, SecretSubmission
from p_mc_optimizer.llm.provider import prompt_bundle_manifest
from p_mc_optimizer.llm.storage import job_call_metadata
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.persistence.repository import json_text,utc_now
from p_mc_optimizer.reporting import html_bytes,json_bytes
from p_mc_optimizer.reporting.evidence_v3 import core_hash,canonical_bytes,value_hash
from p_mc_optimizer.reporting.narrative_v3 import apply_narrative_v3
from p_mc_optimizer.runtime_provenance import get_runtime,source_manifest
from p_mc_optimizer.workflow.narrative_v3 import NarrativeOrchestrator

SCENARIO_LIMITS={'L1':4,'L2':4,'L3':4,'L4':4,'A1':1,'A2':1,'E1':8}
PRIVATE={'credential_ref','credential_generation','api_key','api_token','authorization','headers','base_url','provider_snapshot'}


def safe(value):
    if isinstance(value,dict):return {k:safe(v) for k,v in value.items() if k.casefold() not in PRIVATE}
    if isinstance(value,list):return [safe(v) for v in value]
    return value


def dump(path,value,broker=None):
    value=safe(value)
    if broker is not None:value=broker.sanitize(value)
    tmp=path.with_suffix(path.suffix+'.part');tmp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+'\n');tmp.replace(path)


def reserve_campaign_call(path,scenario,invocation_id,request_hash,role,schema_id):
    """Charge before an external attempt; a replay with the same binding is free."""
    if scenario not in SCENARIO_LIMITS:raise ValueError('Unknown bounded campaign scenario')
    path=Path(path)
    with FileLock(str(path)+'.lock'):
        ledger=json.loads(path.read_text())
        attempts=ledger.setdefault('attempts',[])
        previous=next((row for row in attempts if row.get('invocation_id')==invocation_id),None)
        if previous:
            if previous.get('request_hash')!=request_hash or previous.get('scenario_id')!=scenario:raise ValueError('Invocation replay has changed its request')
            return False
        charged=len(attempts)
        if charged>=min(26,ledger.get('limit_role_calls',26)) or sum(row.get('scenario_id')==scenario for row in attempts)>=SCENARIO_LIMITS[scenario]:raise RuntimeError('CAMPAIGN_ROLE_BUDGET_EXHAUSTED')
        attempts.append({'scenario_id':scenario,'invocation_id':invocation_id,'request_hash':request_hash,'role':role,'schema_id':schema_id,'status':'reserved','reserved_at_utc':datetime.now(UTC).isoformat()})
        ledger['role_calls']=len(attempts)
        tmp=path.with_suffix(path.suffix+'.part');tmp.write_text(json.dumps(ledger,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)
    return True


def record_campaign_result(path,invocation_id,result,metadata):
    path=Path(path)
    with FileLock(str(path)+'.lock'):
        ledger=json.loads(path.read_text());row=next(x for x in ledger['attempts'] if x.get('invocation_id')==invocation_id)
        row.update(status=result.get('status','unknown'),call_id=result.get('call_id'),error_code=(result.get('error') or {}).get('error_code'),finished_at_utc=datetime.now(UTC).isoformat(),usage=(metadata or {}).get('usage'),elapsed_seconds=(metadata or {}).get('elapsed_seconds'),cost=(metadata or {}).get('cost'))
        tmp=path.with_suffix(path.suffix+'.part');tmp.write_text(json.dumps(ledger,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


def campaign_remaining(path,scenario):
    """Use persistent charges from every run, including unresolved attempts."""
    ledger=json.loads(Path(path).read_text());attempts=ledger.get('attempts',[])
    global_remaining=min(26,ledger.get('limit_role_calls',26))-len(attempts)
    scenario_remaining=SCENARIO_LIMITS[scenario]-sum(row.get('scenario_id')==scenario for row in attempts)
    return max(0,min(global_remaining,scenario_remaining))


def offline_writer_draft(context):
    """Controlled fixture prose, deliberately not a live quality evaluation."""
    sections=[]
    for requirement in context['evidence']['sections']:
        sid=requirement['section_id']
        if sid not in context['section_scope'] or not requirement['required']:continue
        refs=requirement['required_fact_refs']
        text={'problem':'مسئلهٔ ثبت‌شده و دامنهٔ آن چنین است: ','candidate_rationale':'روش‌های زیر مطابق پیشنهاد ثبت‌شده بررسی شدند: ','selection':'مبنای تصمیم این اجرا و داده‌های مقایسه عبارت‌اند از: ','solution':'جواب ثبت‌شده و منشأ آن در این اجرا چنین است: ','verification_and_limits':'وضعیت بازبینی و حساب مصرف مستقل از اثبات بهینگی عبارت‌اند از: '}[sid]
        text+='؛ '.join('{{fact:'+ref+'}}' for ref in refs)+'.'
        ref={'problem':'report:problem','candidate_rationale':'report:proposal','selection':'report:decision','solution':'report:solution','verification_and_limits':'report:verification'}[sid]
        sections.append({'section_id':sid,'paragraphs':[{'paragraph_id':sid,'claim_kind':'recorded','text_fa':text,'fact_refs':refs,'evidence_refs':[ref]}]})
    return {'schema_version':'3.0','context_hash':context['context_hash'],'request_hash':context['request_hash'],'sections':sections}


def mock_provider(request):
    body=json.loads(request.content);schema=body['response_format']['json_schema']['name']
    if schema=='connection_test_v1':value={'ok':True}
    else:
        context=json.loads(body['messages'][-1]['content'])
        if schema=='explainer_v3':value=offline_writer_draft(context)
        elif schema=='narrative_review_v1':value={'schema_version':'1.0','context_hash':context['context_hash'],'request_hash':context['request_hash'],'draft_hash':context['draft_hash'],'section_reviews':[{'section_id':sid,'verdict':'accept','issues':[]} for sid in context['section_scope']]}
        else:raise AssertionError('Unexpected reporting fixture schema: '+schema)
    return httpx.Response(200,json={'id':'OFFLINE-NARRATIVE-FIXTURE','model':'offline-fixture','choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(value,ensure_ascii=False)},'finish_reason':'stop'}]})


def copy_profile_tables(source,repo):
    """Copy only legitimate profile/credential refs and prior connection proofs."""
    source=Path(source)/'state.sqlite3'
    if not source.is_file():raise RuntimeError('PROVIDER_CONFIGURATION_REQUIRED: saved profile datastore missing')
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src,repo.transaction() as dst:
        src.row_factory=sqlite3.Row
        for table in ('credential_refs','provider_profiles','active_provider_profile'):
            for row in src.execute('SELECT * FROM '+table):
                fields=list(row.keys());dst.execute('INSERT INTO '+table+' ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',tuple(row))
        for row in src.execute("SELECT * FROM llm_calls WHERE purpose='connection_test'"):
            fields=list(row.keys());dst.execute('INSERT INTO llm_calls ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',tuple(row))


def setup_offline_profile(gateway):
    existing=gateway.get_profile()
    p=existing if existing.get('profile') else gateway.update_profile(ProfileUpdate.model_validate({'expected_revision':None,'preset':'custom_openai_compatible','base_url':'https://offline.example.test/v1','model':'offline-fixture','protocol':'chat_completions_v1','response_mode':'json_schema','token_limit_field':'max_completion_tokens','max_output_tokens':4096,'connection_test_max_output_tokens':256,'request_timeout_seconds':5}))
    profile=p['profile'];target={'profile_id':profile['profile_id'],'profile_revision':profile['revision'],'expected_credential_generation':p['credential_status']['credential_generation'],'target_job_id':None}
    gateway.mutate_credential(SecretSubmission.model_validate({**target,'api_key':'OFFLINE-NARRATIVE-FIXTURE-KEY','storage_mode':'session'}))
    # Local mock only: this is not a live connection test or campaign role call.
    refreshed=gateway.get_profile();target['expected_credential_generation']=refreshed['credential_status']['credential_generation']
    gateway.test_profile(CredentialMutationTarget.model_validate(target),str(uuid4()))


def prepare_qa_job(repo,report,snapshot):
    job_id=report['job_id'];now=utc_now();identity=get_runtime()
    marker={'explanation':'grounded_narrative_v3','writer_schema':'explainer.v3','reviewer_schema':'narrative_review.v1','evidence_builder_version':'1.0','renderer_version':'3.0.0','formatter_version':'1.0'}
    with repo.transaction() as conn:
        if conn.execute('SELECT 1 FROM jobs WHERE job_id=?',(job_id,)).fetchone():return
        policy=deepcopy(report['policy']);policy.update(max_llm_calls=4,max_llm_calls_per_role=2,explain_via_llm=True)
        conn.execute("INSERT INTO jobs(job_id,input,policy,llm_profile,job_status,phase,workflow,created_at,updated_at) VALUES(?,?,?,?,'running','reporting',?,?,?)",(job_id,json_text(report.get('input') or {}),json_text(policy),json_text(snapshot),json_text({'execution_contracts':marker,'runtime:creator':identity}),now,now))


def run_report_only(fixture,output_dir,*,mode='offline',data_dir=None,campaign_ledger=None,scenario='L1'):
    started=monotonic()
    out=Path(output_dir).resolve();out.mkdir(parents=True,exist_ok=True)
    original=Path(fixture).read_bytes();report=json.loads(original);before=core_hash(report)
    repo=Repository(out/'_private_qa')
    with repo.connect() as conn:configured=bool(conn.execute('SELECT 1 FROM active_provider_profile').fetchone())
    if mode=='live' and not configured:copy_profile_tables(data_dir or Path.home()/'Library/Application Support/P_MC_Optimizer',repo)
    gateway=LLMGateway(repo,transport=httpx.MockTransport(mock_provider) if mode=='offline' else None)
    if mode=='offline':setup_offline_profile(gateway)
    campaign_remaining_at_start=None
    if mode=='live':
        if campaign_ledger is None:raise RuntimeError('A persistent --campaign-ledger is required in live mode')
        campaign_remaining_at_start=campaign_remaining(campaign_ledger,scenario)
    active=gateway.get_profile();profile=active.get('profile')
    if profile is None or not active.get('credential_status',{}).get('available'):raise RuntimeError('PROVIDER_CONFIGURATION_REQUIRED: saved key is unavailable; no connection test performed')
    snapshot=gateway.profile_snapshot(profile['profile_id'],profile['revision'])
    prepare_qa_job(repo,report,snapshot)
    job_id=report['job_id']
    def persist(journal):repo.put_workflow(job_id,'narrative_v3_journal',journal)
    def budget():
        with repo.connect() as conn:
            rows=conn.execute('SELECT role FROM llm_calls WHERE job_id=?',(job_id,)).fetchall()
        used={role:sum(row['role']==role for row in rows) for role in ['explainer','report_reviewer']};maximum=4
        if mode=='live':
            if campaign_ledger is None:raise RuntimeError('A persistent --campaign-ledger is required in live mode')
            maximum=min(4,len(rows)+campaign_remaining(campaign_ledger,scenario))
        return {'total_used':len(rows),'by_role':used,'max_total':maximum,'max_per_role':2}
    def invoke(role,schema_id,context,invocation_id):
        if mode=='live':reserve_campaign_call(campaign_ledger,scenario,invocation_id,context['request_hash'],role,schema_id)
        workflow=repo.get_job(job_id)['workflow'];producers=workflow.get('runtime:invocation_producers',{});identity=get_runtime();producers[invocation_id]={'process_run_id':identity['process_run_id'],'role':identity['role'],'llm_role':role,'schema_id':schema_id};repo.put_workflow(job_id,'runtime:invocation_producers',producers)
        result=gateway.invoke(Invocation.model_validate({'job_id':job_id,'role':role,'schema_id':schema_id,'invocation_id':invocation_id,'context':context}))
        if mode=='live':
            with repo.connect() as conn:row=conn.execute('SELECT metadata FROM llm_calls WHERE invocation_id=?',(invocation_id,)).fetchone()
            record_campaign_result(campaign_ledger,invocation_id,result,json.loads(row['metadata'] or '{}') if row else {})
        return result
    counters={'objective_calls':0,'execution_calls':0}
    def forbidden(which):
        def fail(*args,**kwargs):counters[which]+=1;raise AssertionError('report-only attempted '+which)
        return fail
    with repo.connect() as conn:journal=json.loads(conn.execute('SELECT workflow FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]).get('narrative_v3_journal')
    with ExitStack() as stack:
        stack.enter_context(patch('p_mc_optimizer.execution.evaluator.BudgetedEvaluator.__call__',side_effect=forbidden('objective_calls')))
        stack.enter_context(patch('p_mc_optimizer.execution.supervisor.run_execution',side_effect=forbidden('execution_calls')))
        payload=NarrativeOrchestrator(report,invoke=invoke,persist=persist,journal=journal,budget=budget,current_report=lambda:deepcopy(report),prompt_bundle_sha256=value_hash(prompt_bundle_manifest())).run()
        generated=apply_narrative_v3(report,payload)
        generated['source_run_llm_calls']=deepcopy(report.get('llm_calls'))
        generated['source_run_timing']=deepcopy(report.get('timing'))
        generated['source_run_cost_estimate']=deepcopy(report.get('cost_estimate'))
        generated['llm_calls']=job_call_metadata(repo,job_id)
        generated['cost_estimate']=None
        generated['timing']={**(generated.get('timing') or {}),'elapsed_wall_seconds':monotonic()-started,'elapsed_llm_seconds':sum(c.get('elapsed_seconds') or 0 for c in generated['llm_calls']),'elapsed_user_wait_seconds':0.0,'elapsed_research_seconds':0.0}
        generated['qa_reporting']={'scope':'report_only','source_job_id':job_id,'source_fixture_sha256':sha256(original).hexdigest(),'ledger_scope':'fresh_isolated_qa','source_run_fields':['source_run_llm_calls','source_run_timing','source_run_cost_estimate'],'numeric_compute_time_scope':'historical_source_run','wall_and_llm_time_scope':'current_report_only_qa','mode':mode}
        assert core_hash(generated)==before
        dump(out/'report.json',generated,gateway.broker)
        (out/'report.html').write_bytes(html_bytes(generated))
        reopened=json.loads((out/'report.json').read_text());assert core_hash(reopened)==before
        (out/'reopened.html').write_bytes(html_bytes(reopened))
    with repo.connect() as conn:
        rows=[dict(row) for row in conn.execute('SELECT call_id,invocation_id,role,attempt_no,status,input,output,previous_output,error,metadata,started_at,finished_at FROM llm_calls WHERE job_id=? ORDER BY started_at,rowid',(job_id,))]
        current=repo.get_job(job_id)['workflow']['narrative_v3_journal']
        assert conn.execute('SELECT COUNT(*) FROM stage_executions WHERE job_id=?',(job_id,)).fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM trials WHERE job_id=?',(job_id,)).fetchone()[0]==0
        mock_connection_calls=conn.execute("SELECT COUNT(*) FROM llm_calls WHERE purpose='connection_test'").fetchone()[0] if mode=='offline' else 0
    for row in rows:
        for key in ['input','output','error','metadata']:row[key]=json.loads(row[key]) if row[key] else None
    dump(out/'invocations.json',rows,gateway.broker);dump(out/'journal.json',current,gateway.broker)
    capability_passed=payload['mode']=='llm' and payload['review_status']=='passed'
    campaign_blocked=campaign_remaining_at_start is not None and campaign_remaining_at_start<2
    summary={'schema_version':'narrative-report-only/1.0','status':'PASS' if capability_passed else 'BLOCKED' if generated.get('explanation_status')=='provider_error' or campaign_blocked else 'FAIL','mode':mode,'fixture_sha256':sha256(original).hexdigest(),'job_id':job_id,'source_core_before':before,'source_core_after':core_hash(generated),'numerical_core_unchanged':True,'explanation_mode':payload['mode'],'review_status':payload['review_status'],'accepted_required_llm_sections':[x['section']['section_id'] for x in payload['accepted_sections'] if x['origin']=='llm'],'role_calls':len(rows),'external_role_calls':len(rows) if mode=='live' else 0,'connection_calls':0,'external_connection_calls':0,'mock_connection_calls':mock_connection_calls,'objective_evaluations':0,'counters':counters,'campaign_remaining_at_start':campaign_remaining_at_start,'source_manifest_sha256':value_hash(source_manifest()),'prompt_bundle_sha256':value_hash(prompt_bundle_manifest()),'profile':{k:profile[k] for k in ['profile_id','revision','model','request_timeout_seconds','max_output_tokens']},'qa_datastore_private':'_private_qa excluded from distributable artifacts'}
    dump(out/'summary.json',summary,gateway.broker)
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--fixture',required=True,type=Path);p.add_argument('--mode',choices=['offline','live'],required=True);p.add_argument('--output-dir',required=True,type=Path);p.add_argument('--data-dir',type=Path);p.add_argument('--campaign-ledger',type=Path);p.add_argument('--scenario',choices=list(SCENARIO_LIMITS),default='L1');args=p.parse_args()
    try:
        summary=run_report_only(args.fixture,args.output_dir,mode=args.mode,data_dir=args.data_dir,campaign_ledger=args.campaign_ledger,scenario=args.scenario);print(json.dumps(summary,ensure_ascii=False));return 0 if summary['status']=='PASS' else 1
    except Exception as error:
        blocked=(isinstance(error,ApiError) and error.error_code in {'PROVIDER_CONFIGURATION_REQUIRED','PROVIDER_CONNECTION_ERROR','PROVIDER_TIMEOUT','LLM_AUTHENTICATION_FAILED','SECURE_KEYRING_UNAVAILABLE'}) or 'PROVIDER_CONFIGURATION_REQUIRED' in str(error) or 'BUDGET_EXHAUSTED' in str(error)
        args.output_dir.mkdir(parents=True,exist_ok=True);dump(args.output_dir/'blocker.json',{'status':'BLOCKED' if blocked else 'FAIL','error_type':type(error).__name__,'reason':str(error)[:350]});raise

if __name__=='__main__':raise SystemExit(main())
