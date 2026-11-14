#!/usr/bin/env python3
"""Bounded report-only closure campaign. Never evaluates or solves a problem.

Offline prepares isolated fixtures/mocks. Live needs the same committed freeze,
explicit resume and human quality/cause reviews. Verify is strictly read-only.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
from time import monotonic
from unittest.mock import patch
from uuid import uuid4
import csv

import httpx
from filelock import FileLock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.config import data_directory
from p_mc_optimizer.llm.gateway import LLMGateway
from p_mc_optimizer.llm.models import Invocation
from p_mc_optimizer.llm.narrative_models import REQUIRED_SECTIONS, SECTION_ORDER
from p_mc_optimizer.llm.narrative_policy import freeze_narrative_policy
from p_mc_optimizer.llm.provider import prompt_bundle_manifest
from p_mc_optimizer.llm.storage import job_call_metadata
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.persistence.repository import json_text, utc_now
from p_mc_optimizer.reporting import html_bytes
from p_mc_optimizer.reporting.evidence_v3 import build_evidence_v3, core_hash, value_hash, canonical_bytes
from p_mc_optimizer.reporting.narrative_v3 import apply_narrative_v3
from p_mc_optimizer.reporting.narrative_validation import render_sections, validate_draft, validate_review
from p_mc_optimizer.runtime_provenance import get_runtime
from p_mc_optimizer.workflow.narrative_v3 import NarrativeOrchestrator

ORDER = ('C-A2', 'C-A1', 'C-L1', 'C-L4', 'C-L2', 'C-L3', 'C-E1-R')
REPORT_CASES = frozenset({'C-L1','C-L2','C-L3','C-L4','C-E1-R'})
CAPS = {s: 4 if s in REPORT_CASES else 1 for s in ORDER}
STATUSES = frozenset({'PASS','FAIL','BLOCKED','NOT_RUN'})
ENV_ERRORS = frozenset({'PROVIDER_CONFIGURATION_REQUIRED','PROVIDER_AUTH_FAILED','PROVIDER_UNREACHABLE',
    'PROVIDER_TIMEOUT','PROVIDER_RATE_LIMITED','PROVIDER_SERVER_ERROR','SECURE_KEYRING_UNAVAILABLE',
    'LLM_OUTCOME_UNKNOWN','LLM_INVOCATION_PENDING'})
USAGE_COLUMNS = ('campaign_id','scenario_id','invocation_id','call_id','role','operation','request_hash',
    'request_body_sha256','source_manifest_sha256','prompt_bundle_sha256','generation_policy_hash',
    'profile_revision','effective_max_output_tokens','reserved_at','dispatched_at','finished_at',
    'status','error_code','finish_reason','prompt_tokens','completion_tokens','total_tokens','usage_status',
    'monetary_cost','cost_currency','cost_status')


def now(): return datetime.now(UTC).isoformat()
def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def file_hash(path): return sha256(Path(path).read_bytes()).hexdigest()
def git(*args): return subprocess.check_output(['git',*args],cwd=ROOT,text=True).strip()


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);sys.modules[name]=result;spec.loader.exec_module(result)
    return result


def legacy_helpers(): return module('closure_legacy_helpers',ROOT/'scripts/qa_narrative_v3.py')


def cases_module():
    return module('closure_reviewer_cases',ROOT/'tests/fixtures/narrative_closure/cases.py')


def redact(value, path=()):
    """Remove actual credential paths, preserving numerical authorization records."""
    if isinstance(value,dict):
        output={}
        for key,item in value.items():
            low=key.casefold()
            numerical=low=='authorization' and path and path[-1]=='numeric_execution'
            if low in {'api_key','api_token','credential_ref','credential_generation','cookie','cookies'}: continue
            if low in {'authorization','headers'} and not numerical: continue
            if low=='base_url' and ('llm_profile' in path or 'provider_snapshot' in path): continue
            output[key]=redact(item,(*path,key))
        return output
    if isinstance(value,list):return [redact(v,(*path,str(i))) for i,v in enumerate(value)]
    return value


def dump(path,value,broker=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    output=redact(value)
    if broker is not None:output=broker.sanitize(output)
    data=json.dumps(output,ensure_ascii=False,allow_nan=False,indent=2)+'\n'
    temp=path.with_suffix(path.suffix+'.part');temp.write_text(data,encoding='utf-8');temp.replace(path)


def runtime_manifest():
    paths=set()
    for folder in ('src/p_mc_optimizer','schemas'):
        paths.update(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc' and p.name!='.DS_Store')
    for name in ('run_server.py','pyproject.toml','requirements.lock','requirements-test.lock',
                 'scripts/start.sh','scripts/start.bat','scripts/setup.sh','scripts/setup.bat','examples/config.example.json'):
        if (ROOT/name).is_file():paths.add(ROOT/name)
    files=[{'path':p.relative_to(ROOT).as_posix(),'sha256':file_hash(p),'bytes':p.stat().st_size} for p in sorted(paths)]
    return {'files':files,'canonical_source_sha256':value_hash(files)}


def harness_manifest():
    paths=[ROOT/'scripts/qa_narrative_v3_closure.py',ROOT/'scripts/qa_narrative_v3.py']
    paths.extend(p for p in (ROOT/'tests').rglob('*') if p.is_file() and p.suffix in {'.py','.json','.txt'} and '__pycache__' not in p.parts)
    files=[{'path':p.relative_to(ROOT).as_posix(),'sha256':file_hash(p)} for p in sorted(set(paths))]
    return {'files':files,'canonical_harness_sha256':value_hash(files)}


def case_source(root,scenario):
    return root/'inputs'/({'C-A1':'F1','C-A2':'F1','C-E1-R':'E1'}.get(scenario,scenario.replace('C-L','F'))+'.json')


def new_ledger(campaign):
    return {'schema_version':'narrative-closure-ledger/1.0','campaign_id':campaign,'limit':22,
            'scenario_caps':CAPS,'order':list(ORDER),'attempts':[], 'external_dispatches':0,
            'fresh_objective_evaluations':0,'solver_calls':0,'state':'prepared','stop_reason':None}


def reserve_attempt(root,scenario,invocation,request_hash,role,operation,source_hash,prompt_hash):
    if scenario not in ORDER or role not in {'explainer','report_reviewer'} or (scenario not in REPORT_CASES and role!='report_reviewer'):
        raise ValueError('INVALID_CAMPAIGN_ROLE')
    path=root/'campaign_ledger.json'
    with FileLock(str(path)+'.lock'):
        ledger=read(path)
        prior=next((x for x in ledger['attempts'] if x['invocation_id']==invocation),None)
        if prior:
            if (prior['scenario_id'],prior['request_hash'],prior['role'])!=(scenario,request_hash,role):raise ValueError('INVOCATION_CONFLICT')
            return False
        if ledger['state'] in {'failed','blocked','complete'}:raise RuntimeError('CAMPAIGN_CLOSED')
        rows=[x for x in ledger['attempts'] if x['scenario_id']==scenario]
        if scenario not in CAPS or len(ledger['attempts'])>=22 or len(rows)>=CAPS[scenario]:raise RuntimeError('CAMPAIGN_BUDGET_EXHAUSTED')
        if sum(x['role']==role for x in rows)>=(2 if scenario in REPORT_CASES else 1):raise RuntimeError('SCENARIO_ROLE_BUDGET_EXHAUSTED')
        if role=='explainer' and (CAPS[scenario]-len(rows)<2 or sum(x['role']=='report_reviewer' for x in rows)>=2):raise RuntimeError('NO_RESERVED_REVIEW_CAPACITY')
        ledger['attempts'].append({'campaign_id':ledger['campaign_id'],'scenario_id':scenario,'invocation_id':invocation,
            'call_id':None,'role':role,'operation':operation,'request_hash':request_hash,'request_body_sha256':None,
            'source_manifest_sha256':source_hash,'prompt_bundle_sha256':prompt_hash,
            'generation_policy_hash':freeze_narrative_policy('1.1')['report_generation_policy_hash'],
            'profile_revision':None,'effective_max_output_tokens':None,'reserved_at':now(),'dispatched_at':None,
            'finished_at':None,'status':'reserved','error_code':None,'finish_reason':None,
            'prompt_tokens':None,'completion_tokens':None,'total_tokens':None,'usage_status':'unavailable',
            'monetary_cost':None,'cost_currency':None,'cost_status':'unavailable'})
        ledger['state']='running';dump(path,ledger)
    return True


def update_attempt(root,invocation,updates):
    path=root/'campaign_ledger.json'
    with FileLock(str(path)+'.lock'):
        ledger=read(path);row=next(x for x in ledger['attempts'] if x['invocation_id']==invocation)
        row.update(updates);ledger['external_dispatches']=sum(x['dispatched_at'] is not None for x in ledger['attempts']);dump(path,ledger)


def close_campaign(root,state,reason):
    path=root/'campaign_ledger.json'
    with FileLock(str(path)+'.lock'):
        ledger=read(path);ledger.update(state=state,stop_reason=reason);dump(path,ledger)


def write_usage(root):
    ledger=read(root/'campaign_ledger.json')
    with (root/'call_usage.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=USAGE_COLUMNS);writer.writeheader()
        writer.writerows({k:row.get(k) for k in USAGE_COLUMNS} for row in ledger['attempts'])


class DispatchTransport(httpx.BaseTransport):
    def __init__(self,root,repo):
        self.root,self.repo=root,repo;self.invocation=None
        self.inner=httpx.HTTPTransport(retries=0,verify=True,trust_env=False)
    def fatal(self,code):
        close_campaign(self.root,'failed',code)
        raise RuntimeError(code)
    def handle_request(self,request):
        if not self.invocation:self.fatal('UNBOUND_EXTERNAL_DISPATCH')
        with self.repo.connect() as conn:
            row=conn.execute('SELECT call_id,metadata,profile_revision FROM llm_calls WHERE invocation_id=?',(self.invocation,)).fetchone()
        if row is None:self.fatal('DISPATCH_WITHOUT_DURABLE_GATEWAY_RESERVATION')
        metadata=json.loads(row['metadata'] or '{}');proof=metadata.get('provenance') or {}
        expected_policy=freeze_narrative_policy('1.1')
        if (proof.get('report_generation_policy_version')!='1.1' or
                proof.get('report_generation_policy_hash')!=expected_policy['report_generation_policy_hash']):self.fatal('DISPATCH_POLICY_MISMATCH')
        actual=sha256(request.content).hexdigest()
        if proof.get('request_body_sha256')!=actual:self.fatal('DISPATCH_BODY_MISMATCH')
        ledger=read(self.root/'campaign_ledger.json');attempt=next(x for x in ledger['attempts'] if x['invocation_id']==self.invocation)
        if attempt['dispatched_at'] is not None:self.fatal('HIDDEN_EXTERNAL_RETRY')
        body=json.loads(request.content);cap=body.get('max_tokens',body.get('max_completion_tokens'))
        if cap!=(8192 if attempt['role']=='explainer' else 4096):self.fatal('EFFECTIVE_OUTPUT_CAP_MISMATCH')
        update_attempt(self.root,self.invocation,{'call_id':row['call_id'],'request_body_sha256':actual,
            'profile_revision':row['profile_revision'],'effective_max_output_tokens':cap,'dispatched_at':now(),
            'dispatch_semantics':'entered real HTTPTransport; provider receipt can remain unknown'})
        return self.inner.handle_request(request)
    def close(self):pass  # provider closes each Client; keep this bounded pool until runner teardown
    def finish(self):self.inner.close()


@contextmanager
def no_numerics(root=None):
    counters={'objective_calls':0,'solver_calls':0}
    def forbidden(which):
        def call(*args,**kwargs):
            counters[which]+=1
            if root is not None:
                path=root/'campaign_ledger.json'
                with FileLock(str(path)+'.lock'):
                    ledger=read(path);key='objective_call_guard_violations' if which=='objective_calls' else 'solver_call_guard_violations'
                    ledger[key]=ledger.get(key,0)+1;ledger.update(state='failed',stop_reason='REPORT_ONLY_NUMERICAL_CALL_FORBIDDEN:'+which);dump(path,ledger)
            raise RuntimeError('REPORT_ONLY_NUMERICAL_CALL_FORBIDDEN:'+which)
        return call
    with ExitStack() as stack:
        stack.enter_context(patch('p_mc_optimizer.execution.evaluator.BudgetedEvaluator.__call__',side_effect=forbidden('objective_calls')))
        stack.enter_context(patch('p_mc_optimizer.execution.supervisor.run_execution',side_effect=forbidden('solver_calls')))
        yield counters


def contracts():
    return {'explanation':'grounded_narrative_v3','writer_schema':'explainer.v3','reviewer_schema':'narrative_review.v1',
        'report_generation_policy_version':'1.1','report_generation_policy':freeze_narrative_policy('1.1'),
        'evidence_builder_version':'1.1','formatter_version':'1.1','renderer_version':'3.0.0'}


def prepare_job(repo,report,snapshot):
    job_id=report['job_id']
    with repo.connect() as db:existing=db.execute('SELECT 1 FROM jobs WHERE job_id=?',(job_id,)).fetchone()
    if existing:return
    policy=deepcopy(report['policy']);policy.update(max_llm_calls=4,max_llm_calls_per_role=2,explain_via_llm=True)
    identity=get_runtime();stamp=utc_now()
    with repo.transaction() as db:
        db.execute("INSERT INTO jobs(job_id,input,policy,llm_profile,job_status,phase,workflow,created_at,updated_at) VALUES(?,?,?,?,'running','reporting',?,?,?)",(job_id,json_text(report['input']),json_text(policy),json_text(snapshot),json_text({'execution_contracts':contracts(),'runtime:creator':identity}),stamp,stamp))


def profile_summary(gateway):
    status=gateway.get_profile();profile=status.get('profile')
    if not profile or not status.get('credential_status',{}).get('available') or profile.get('test_status')!='passed':raise RuntimeError('PROVIDER_CONFIGURATION_REQUIRED')
    snapshot=gateway.profile_snapshot(profile['profile_id'],profile['revision'])
    return {**{k:profile[k] for k in ('profile_id','revision','preset','model','protocol','response_mode','token_limit_field','max_output_tokens','request_timeout_seconds')},
        'provider_snapshot_sha256':snapshot['provider_snapshot_hash'],
        'credential_generation_at_freeze':status['credential_status']['credential_generation']}


def make_gateway(root,scenario,report,mode,provider_data):
    repo=Repository(root/'_private_qa'/mode/scenario)
    helpers=legacy_helpers()
    with repo.connect() as db:configured=bool(db.execute('SELECT 1 FROM active_provider_profile').fetchone())
    transport=None
    if mode=='offline':
        gateway=LLMGateway(repo,transport=httpx.MockTransport(helpers.mock_provider));helpers.setup_offline_profile(gateway)
    else:
        if not configured:helpers.copy_profile_tables(provider_data,repo)
        transport=DispatchTransport(root,repo);gateway=LLMGateway(repo,transport=transport)
    summary=profile_summary(gateway)
    if mode=='live':
        frozen=read(root/'freeze.json')['provider_profile']
        if summary!=frozen:raise RuntimeError('FROZEN_PROVIDER_PROFILE_MISMATCH')
    prepare_job(repo,report,gateway.profile_snapshot(summary['profile_id'],summary['revision']))
    return repo,gateway,transport


def invocation_callback(root,scenario,repo,gateway,transport,source_report,mode):
    def invoke(role,schema_id,context,invocation_id):
        assert core_hash(source_report)==read(root/'fixtures_manifest.json')['cases'][scenario]['source_core_sha256']
        if mode=='live':
            validate_freeze(root)
            workflow=repo.get_job(source_report['job_id'])['workflow'];active=workflow['narrative_v3_journal']['active_request']
            reserve_attempt(root,scenario,invocation_id,context['request_hash'],role,active['operation'],
                read(root/'freeze.json')['runtime']['canonical_source_sha256'],value_hash(prompt_bundle_manifest()))
            transport.invocation=invocation_id
        identity=get_runtime();producers=repo.get_job(source_report['job_id'])['workflow'].get('runtime:invocation_producers',{})
        producers[invocation_id]={'process_run_id':identity['process_run_id'],'role':identity['role'],'llm_role':role,'schema_id':schema_id}
        repo.put_workflow(source_report['job_id'],'runtime:invocation_producers',producers)
        try:
            result=gateway.invoke(Invocation.model_validate({'job_id':source_report['job_id'],'role':role,'schema_id':schema_id,'invocation_id':invocation_id,'context':context}))
        except Exception as error:
            if mode=='live':
                row=next(r for r in read(root/'campaign_ledger.json')['attempts'] if r['invocation_id']==invocation_id)
                status='failed' if isinstance(error,ApiError) and row['dispatched_at'] is None else 'outcome_unknown'
                update_attempt(root,invocation_id,{'finished_at':now(),'status':status,'error_code':getattr(error,'error_code',type(error).__name__)})
                write_usage(root)
            raise
        if mode=='live':
            metadata=result.get('metadata') or {};proof=metadata.get('provenance') or {};usage=metadata.get('usage') or {}
            update_attempt(root,invocation_id,{'call_id':result.get('call_id'),'finished_at':now(),'status':result['status'],
                'error_code':(result.get('error') or {}).get('error_code'),'finish_reason':metadata.get('finish_reason'),
                'request_body_sha256':proof.get('request_body_sha256'),'effective_max_output_tokens':proof.get('effective_max_output_tokens',metadata.get('effective_max_output_tokens')),
                'profile_revision':repo.get_job(source_report['job_id'])['llm_profile']['revision'],
                'prompt_tokens':usage.get('prompt_tokens'),'completion_tokens':usage.get('completion_tokens'),'total_tokens':usage.get('total_tokens'),
                'usage_status':metadata.get('usage_status','unavailable'),'elapsed_seconds':metadata.get('elapsed_seconds')})
            write_usage(root)
            current_ledger=read(root/'campaign_ledger.json')
            if current_ledger['state']=='failed':raise RuntimeError(current_ledger['stop_reason'])
            validate_freeze(root)
        assert core_hash(source_report)==read(root/'fixtures_manifest.json')['cases'][scenario]['source_core_sha256']
        return result
    return invoke


def export_report(root,scenario,repo,gateway,source,payload,mode,counters):
    out=root/('offline/mock' if mode=='offline' else 'live')/scenario;out.mkdir(parents=True,exist_ok=True)
    before=core_hash(source);generated=apply_narrative_v3(source,payload)
    generated['execution_contracts']=contracts();generated['report_generation_policy_version']='1.1'
    generated['source_run_llm_calls']=deepcopy(source.get('llm_calls'));generated['source_run_timing']=deepcopy(source.get('timing'))
    generated['llm_calls']=job_call_metadata(repo,source['job_id']);generated['cost_estimate']=None
    generated['qa_reporting']={'scope':'report_only','campaign_scope':'fresh closure campaign' if mode=='live' else 'offline mock only',
        'historical_numeric_compute_only':True,'fresh_objective_evaluations':0,'solver_calls':0}
    dump(out/'report.json',generated,gateway.broker)
    reopened=read(out/'report.json')
    if core_hash(reopened)!=before:raise RuntimeError('EXPORTED_CORE_CHANGED')
    (out/'report.html').write_bytes(html_bytes(reopened));(out/'reopened.html').write_bytes(html_bytes(read(out/'report.json')))
    if (out/'report.html').read_bytes()!=(out/'reopened.html').read_bytes():raise RuntimeError('REOPEN_CHANGED')
    with repo.connect() as db:journal=json.loads(db.execute('SELECT workflow FROM jobs WHERE job_id=?',(source['job_id'],)).fetchone()[0])['narrative_v3_journal']
    dump(out/'journal.json',journal,gateway.broker)
    dump(out/'evidence.json',payload['evidence'],gateway.broker)
    dump(out/'rendered_sections.json',payload['rendered_sections'],gateway.broker)
    dump(out/'accepted_raw_sections.json',payload['accepted_sections'],gateway.broker)
    dump(out/'invocations.json',job_call_metadata(repo,source['job_id']),gateway.broker)
    if mode=='live' and repo.get_job(source['job_id'])['job_status']!='completed':
        artifacts=[repo.save_artifact(source['job_id'],'report_json',(out/'report.json').read_bytes(),'.json'),repo.save_artifact(source['job_id'],'report_html',(out/'report.html').read_bytes(),'.html')]
        repo.publish_report(source['job_id'],reopened,[a['artifact_id'] for a in artifacts],job_status='completed',solution_status=source['solution_status'],explanation_status=generated['explanation_status'],optimality_status=source['optimality_status'])
    mandatory=sorted({x['section']['section_id'] for x in payload['accepted_sections'] if x['origin']=='llm'}&set(REQUIRED_SECTIONS))
    eligible=len(mandatory)==5 and payload['mode']=='llm' and payload['review_status']=='passed'
    attempts=payload['attempts'];environmental=any(a.get('error_code') in ENV_ERRORS for a in attempts)
    result={'scenario_id':scenario,'case_type':'report_only','status':'NOT_RUN' if eligible and mode=='live' else 'PASS' if eligible else 'BLOCKED' if environmental else 'FAIL',
        'stage':'awaiting_quality_review' if eligible and mode=='live' else 'finished','fresh_live_status':'NOT_RUN' if mode=='offline' or eligible else 'BLOCKED' if environmental else 'FAIL',
        'recorded_response_replay_status':'NOT_RUN','quality_rubric_applicable':True,'quality_rubric':None,
        'five_mandatory_model_sections':mandatory,'mode':payload['mode'],'review_status':payload['review_status'],
        'context_hash':payload['context_hash'],'source_core_sha256_before':before,'source_core_sha256_after':core_hash(reopened),
        'source_manifest_sha256':runtime_manifest()['canonical_source_sha256'],'prompt_bundle_sha256':value_hash(prompt_bundle_manifest()),
        'report_sha256':file_hash(out/'report.json'),'counters':counters,'reason_fa':'انتظار خواندن واقعی متن و ثبت Q1 تا Q6' if eligible and mode=='live' else 'نتیجهٔ واقعی مسیر گزارش، مطابق منشأ و بازبینی ثبت شد.'}
    dump(out/'summary.json',result)
    return result


def run_report(root,scenario,mode,provider_data):
    source=read(case_source(root,scenario));before=core_hash(source)
    repo,gateway,transport=make_gateway(root,scenario,source,mode,provider_data)
    def persist(journal):repo.put_workflow(source['job_id'],'narrative_v3_journal',journal)
    def budget():
        with repo.connect() as db:rows=db.execute('SELECT role FROM llm_calls WHERE job_id=?',(source['job_id'],)).fetchall()
        maximum=4
        if mode=='live':
            ledger=read(root/'campaign_ledger.json');used=[x for x in ledger['attempts'] if x['scenario_id']==scenario]
            maximum=min(4,len(rows)+22-len(ledger['attempts']),len(rows)+CAPS[scenario]-len(used))
        return {'total_used':len(rows),'by_role':{r:sum(x['role']==r for x in rows) for r in ['explainer','report_reviewer']},'max_total':maximum,'max_per_role':2}
    journal=repo.get_job(source['job_id'])['workflow'].get('narrative_v3_journal')
    try:
        with no_numerics(root if mode=='live' else None) as counters:
            payload=NarrativeOrchestrator(source,invoke=invocation_callback(root,scenario,repo,gateway,transport,source,mode),persist=persist,
                journal=journal,budget=budget,current_report=lambda:deepcopy(source),prompt_bundle_sha256=value_hash(prompt_bundle_manifest()),report_generation_policy_version='1.1').run()
            if core_hash(source)!=before:raise RuntimeError('SOURCE_CORE_CHANGED')
            return export_report(root,scenario,repo,gateway,source,payload,mode,counters)
    finally:
        if transport:transport.finish()


def run_adversarial(root,scenario,provider_data):
    source=read(case_source(root,scenario));prepared=read(root/'prepared'/f'{scenario}.json');out=root/'live'/scenario;out.mkdir(parents=True,exist_ok=True)
    repo,gateway,transport=make_gateway(root,scenario,source,'live',provider_data)
    context=prepared['reviewer_context'];evidence=context['evidence']
    prior=repo.get_job(source['job_id'])['workflow'].get('narrative_v3_journal') or {}
    invocation=(prior.get('active_request') or {}).get('invocation_id') or str(uuid4())
    active={'role':'report_reviewer','schema_id':'narrative_review.v1','operation':'review','status':'ready',
        'context':context,'request_hash':context['request_hash'],'invocation_id':invocation}
    repo.put_workflow(source['job_id'],'narrative_v3_journal',{'report_generation_policy_version':'1.1',
        'report_generation_policy':freeze_narrative_policy('1.1'),'report_generation_policy_hash':freeze_narrative_policy('1.1')['report_generation_policy_hash'],
        'evidence':evidence,'context_hash':context['context_hash'],'source_core_sha256':evidence['source_core_sha256'],
        'prompt_bundle_sha256':context['prompt_bundle_sha256'],'active_request':active})
    try:
        with no_numerics(root) as counters:
            result=invocation_callback(root,scenario,repo,gateway,transport,source,'live')('report_reviewer','narrative_review.v1',context,invocation)
            dump(out/'response.json',result,gateway.broker);dump(out/'input.json',prepared,gateway.broker)
            verdicts={};codes={};validation_error=None
            if result['status']=='succeeded':
                try:
                    review=validate_review(context,result['output']);verdicts={r.section_id:r.verdict for r in review.section_reviews}
                    codes={r.section_id:[i.code for i in r.issues] for r in review.section_reviews}
                except Exception as error:validation_error=type(error).__name__
            valid=bool(prepared['expected_accept']) and all(verdicts.get(s)=='accept' for s in prepared['expected_accept']) and all(verdicts.get(s)=='reject' for s in prepared['expected_reject'])
            environmental=(result.get('error') or {}).get('error_code') in ENV_ERRORS
            summary={'scenario_id':scenario,'case_type':'reviewer_adversarial','status':'NOT_RUN' if valid else 'BLOCKED' if environmental else 'FAIL',
                'stage':'awaiting_cause_review' if valid else 'finished','fresh_live_status':'NOT_RUN' if valid else 'BLOCKED' if environmental else 'FAIL',
                'recorded_response_replay_status':'NOT_RUN','quality_rubric':None,'quality_rubric_applicable':False,
                'expected_accept':prepared['expected_accept'],'expected_reject':prepared['expected_reject'],'actual_verdicts':verdicts,'actual_issue_codes':codes,
                'validation_error':validation_error,'context_hash':context['context_hash'],'source_manifest_sha256':runtime_manifest()['canonical_source_sha256'],
                'counters':counters,'reason_fa':'انتظار بررسی ارتباط علت رد با ادعای عمدی' if valid else 'شرط verdict یا قالب معتبر برقرار نشد.'}
            dump(out/'summary.json',summary);return summary
    finally:transport.finish()


def input_manifest(root):
    names={'C-L1':'F1','C-L2':'F2','C-L3':'F3','C-L4':'F4','C-A1':'F1','C-A2':'F1','C-E1-R':'E1'}
    cases={}
    for scenario,name in names.items():
        p=root/'inputs'/f'{name}.json'
        cases[scenario]={'path':f'inputs/{name}.json','available':p.exists(),'file_sha256':file_hash(p) if p.exists() else None,
            'source_core_sha256':core_hash(read(p)) if p.exists() else None,'kind':'synthetic' if name in {'F2','F3','F4'} else 'real_archived'}
    return {'cases':cases}


def prepare(root,fixture_root,archived_e1,resume):
    if root.exists() and any(root.iterdir()) and not resume:raise RuntimeError('EXISTING_OUTPUT_REQUIRES_RESUME')
    root.mkdir(parents=True,exist_ok=True)
    if (root/'run_manifest.json').exists():
        validate_inputs(root,fixture_root,archived_e1)
        if (root/'freeze.json').exists():raise RuntimeError('FROZEN_CAMPAIGN_OFFLINE_PREPARATION_CLOSED')
    else:
        (root/'inputs').mkdir(exist_ok=True)
        for name in ('F1','F2','F3','F4'):
            source=fixture_root/f'{name}.json';target=root/'inputs'/source.name
            target.write_bytes(source.read_bytes())
        if archived_e1.is_file():
            source=read(archived_e1)
            if not source.get('narrative_v3') or core_hash(source)!=source['narrative_v3']['source_core_sha256']:raise RuntimeError('ARCHIVED_E1_CORE_INVALID')
            names={c['algorithm_id'] for c in source['candidate_plan']['candidates']}
            if not any('CMA_ES' in n for n in names) or not any('.ABC:' in n for n in names):raise RuntimeError('ARCHIVED_E1_IS_NOT_REAL_E1')
            (root/'inputs/E1.json').write_bytes(archived_e1.read_bytes())
        campaign=root.name
        dump(root/'run_manifest.json',{'schema_version':'narrative-closure-run/1.0','campaign_id':campaign,'baseline_commit':git('rev-parse','HEAD'),
            'scope':'report-only final narrative closure','created_at':now(),'report_generation_policy':freeze_narrative_policy('1.1'),
            'python_version':platform.python_version(),'limits':{'charged_role_attempts':22,'fresh_objective_evaluations':0,'solver_calls':0}})
        dump(root/'campaign_ledger.json',new_ledger(campaign));write_usage(root)
        dump(root/'fixtures_manifest.json',input_manifest(root))
    (root/'prepared').mkdir(exist_ok=True)
    for scenario in ('C-A1','C-A2'):
        evidence=build_evidence_v3(read(case_source(root,scenario)),policy_version='1.1')
        draft,reviewer,accept,reject,targets=cases_module().make_case(evidence,scenario,value_hash(prompt_bundle_manifest()))
        dump(root/'prepared'/f'{scenario}.json',{'scenario_id':scenario,'case_type':'reviewer_adversarial','draft':draft,
            'reviewer_context':reviewer.model_dump(mode='json'),'rendered_sections':[s.model_dump(mode='json') for s in render_sections(evidence,draft['sections'])],
            'expected_accept':accept,'expected_reject':reject,'target_issue_codes':targets,'deterministic_validation':'PASS'})
    results=[]
    for scenario in ('C-L1','C-L2','C-L3','C-L4','C-E1-R'):
        if case_source(root,scenario).exists():results.append(run_report(root,scenario,'offline',None))
    dump(root/'offline/mock-results.json',{'status':'PASS' if results and all(r['status']=='PASS' for r in results) else 'FAIL',
        'case_count':len(results),'cases':results,'external_dispatches':0,'fresh_objective_evaluations':0,'solver_calls':0,'scope':'controlled MockTransport; not live model quality'})
    mock_rows=[]
    for scenario in ('C-L1','C-L2','C-L3','C-L4','C-E1-R'):
        dbpath=root/'_private_qa/offline'/scenario/'state.sqlite3'
        if dbpath.exists():
            with sqlite3.connect(dbpath) as conn:
                conn.row_factory=sqlite3.Row
                for row in conn.execute('SELECT invocation_id,role,status,metadata FROM llm_calls WHERE job_id IS NOT NULL ORDER BY started_at,rowid'):
                    mock_rows.append({'scenario_id':scenario,'scope':'offline_mock','external_dispatch':False,**dict(row)})
    dump(root/'offline/mock_ledger.json',{'attempts':mock_rows,'external_dispatches':0,'not_part_of_live_ledger':True})
    dump(root/'offline/preparation.json',{'status':'PASS' if results and all(r['status']=='PASS' for r in results) else 'FAIL',
        'runtime':runtime_manifest(),'harness':harness_manifest(),'prompt_bundle_sha256':value_hash(prompt_bundle_manifest()),
        'prepared_cases':{s:file_hash(root/'prepared'/f'{s}.json') for s in ('C-A1','C-A2')},'fresh_objective_evaluations':0,'solver_calls':0})
    return read(root/'offline/preparation.json')


def validate_inputs(root,fixture_root,archived_e1):
    recorded=read(root/'fixtures_manifest.json')
    if input_manifest(root)!=recorded:raise RuntimeError('FROZEN_INPUT_CHANGED')
    for name in ('F1','F2','F3','F4'):
        if file_hash(fixture_root/f'{name}.json')!=file_hash(root/'inputs'/f'{name}.json'):raise RuntimeError('FIXTURE_SOURCE_CHANGED')
    if (root/'inputs/E1.json').exists() and (not archived_e1.exists() or file_hash(archived_e1)!=file_hash(root/'inputs/E1.json')):raise RuntimeError('ARCHIVED_E1_CHANGED')


def validate_freeze(root):
    frozen=read(root/'freeze.json')
    if runtime_manifest()!=frozen['runtime'] or harness_manifest()!=frozen['harness']:raise RuntimeError('FROZEN_SOURCE_OR_HARNESS_CHANGED')
    if value_hash(prompt_bundle_manifest())!=frozen['prompt_bundle_sha256']:raise RuntimeError('FROZEN_PROMPTS_CHANGED')
    if file_hash(root/'fixtures_manifest.json')!=frozen['fixtures_manifest_sha256']:raise RuntimeError('FROZEN_FIXTURE_MANIFEST_CHANGED')
    if input_manifest(root)!=read(root/'fixtures_manifest.json'):raise RuntimeError('FROZEN_INPUT_CHANGED')
    if file_hash(root/'offline/full-final.xml')!=frozen['full_regression_junit_sha256']:raise RuntimeError('FROZEN_REGRESSION_EVIDENCE_CHANGED')
    for case,digest in frozen['prepared_cases'].items():
        if file_hash(root/'prepared'/f'{case}.json')!=digest:raise RuntimeError('FROZEN_ADVERSARIAL_CASE_CHANGED')
    return frozen


def source_provider_summary(root,provider_data):
    """Read legitimate saved configuration in a disposable private copy; no warm-up."""
    repo=Repository(root/'_private_qa'/('provider-observation-'+str(uuid4())))
    legacy_helpers().copy_profile_tables(provider_data,repo)
    return profile_summary(LLMGateway(repo))


def freeze_candidate(root,provider_data):
    """Explicit final offline step, after commit, full regression and initial UI QA."""
    import xml.etree.ElementTree as ET
    if (root/'freeze.json').exists():raise RuntimeError('ONE_CANDIDATE_ONLY')
    if git('status','--porcelain'):raise RuntimeError('FREEZE_REQUIRES_COMMITTED_CLEAN_SOURCE')
    prep=read(root/'offline/preparation.json')
    if prep['status']!='PASS' or prep['runtime']!=runtime_manifest() or prep['harness']!=harness_manifest():raise RuntimeError('PREPARATION_NOT_CURRENT')
    junit=root/'offline/full-final.xml';nodes=list(ET.parse(junit).iter('testcase'))
    if not nodes or any(n.find('failure') is not None or n.find('error') is not None or n.find('skipped') is not None for n in nodes):raise RuntimeError('OFFLINE_REGRESSION_NOT_PASS')
    ui=read(root/'ui/offline-browser-summary.json')
    if ui['status']!='PASS' or ui['source_manifest_sha256']!=runtime_manifest()['canonical_source_sha256']:raise RuntimeError('INITIAL_DISPLAY_QA_NOT_CURRENT')
    healthy=read(root/'offline/adversarial-input-audit.json')
    if healthy['status']!='PASS' or healthy['prepared_cases']!=prep['prepared_cases']:raise RuntimeError('ADVERSARIAL_CONTROLS_NOT_REVIEWED')
    frozen={'schema_version':'narrative-closure-freeze/1.0','tested_source_commit':git('rev-parse','HEAD'),
        'frozen_at':now(),'runtime':runtime_manifest(),'harness':harness_manifest(),
        'prompt_bundle_sha256':value_hash(prompt_bundle_manifest()),'prepared_cases':prep['prepared_cases'],
        'provider_profile':source_provider_summary(root,provider_data),
        'generation_policy':freeze_narrative_policy('1.1'),'full_regression_tests':len(nodes),
        'full_regression_junit_sha256':file_hash(junit),'fixtures_manifest_sha256':file_hash(root/'fixtures_manifest.json')}
    dump(root/'freeze.json',frozen);dump(root/'source_manifest.json',frozen['runtime']);dump(root/'harness_manifest.json',frozen['harness'])
    manifest=read(root/'run_manifest.json');manifest.update(tested_source_commit=frozen['tested_source_commit'],provider_profile=frozen['provider_profile'],
        source_manifest_sha256=frozen['runtime']['canonical_source_sha256'],source_manifest_file_sha256=file_hash(root/'source_manifest.json'),frozen_at=frozen['frozen_at'])
    dump(root/'run_manifest.json',manifest);return frozen


def complete_human_review(root,scenario,summary):
    out=root/'live'/scenario
    if summary['stage']=='awaiting_quality_review':
        path=out/'quality_review.json'
        if not path.exists():return summary
        review=read(path);rows=review.get('rubric',[])
        if len(rows)!=6 or {r['id'] for r in rows}!={f'Q{i}' for i in range(1,7)}:raise RuntimeError('INVALID_QUALITY_RUBRIC')
        if review.get('report_sha256')!=summary['report_sha256']:raise RuntimeError('QUALITY_REPORT_BINDING_MISMATCH')
        paragraph_ids={p['paragraph_id'] for s in read(out/'report.json')['narrative_v3']['rendered_sections'] for p in s['paragraphs']}
        if any(r['status'] not in {'PASS','FAIL'} or not r.get('paragraph_ids') or not set(r['paragraph_ids'])<=paragraph_ids or not r.get('reason_fa') for r in rows):raise RuntimeError('UNSUPPORTED_QUALITY_CLAIM')
        passed=all(r['status']=='PASS' for r in rows);summary['quality_rubric']=rows
    else:
        path=out/'adversarial_audit.json'
        if not path.exists():return summary
        review=read(path)
        if review.get('response_sha256')!=file_hash(out/'response.json') or review.get('context_hash')!=summary['context_hash']:raise RuntimeError('ADVERSARIAL_REVIEW_BINDING_MISMATCH')
        if review.get('status') not in {'PASS','FAIL'} or not review.get('reason_fa'):raise RuntimeError('INVALID_CAUSE_REVIEW')
        passed=review['status']=='PASS';summary['target_cause_audit']=review
    summary.update(status='PASS' if passed else 'FAIL',fresh_live_status='PASS' if passed else 'FAIL',stage='finished',reason_fa=review.get('reason_fa','معیارهای Q1 تا Q6 روی متن واقعی بررسی شدند.'))
    dump(out/'summary.json',summary);return summary


def run_live(root,provider_data):
    frozen=validate_freeze(root);ledger=read(root/'campaign_ledger.json')
    if ledger['state'] in {'failed','blocked','complete'}:return {'state':ledger['state'],'reason':ledger['stop_reason']}
    if source_provider_summary(root,provider_data)!=frozen['provider_profile']:raise RuntimeError('SOURCE_PROVIDER_CONFIGURATION_CHANGED')
    run_identity={'identity':get_runtime(),'full_runtime_manifest_sha256_at_runner_start':runtime_manifest()['canonical_source_sha256'],
        'frozen_tested_commit':frozen['tested_source_commit'],'observed_at':now(),'scope':'actual report-only runner, no app worker or solver'}
    dump(root/'live'/('runner-'+run_identity['identity']['process_run_id']+'.json'),run_identity)
    for scenario in ORDER:
        path=root/'live'/scenario/'summary.json'
        if path.exists():
            summary=read(path)
            if summary['stage'].startswith('awaiting_'):summary=complete_human_review(root,scenario,summary)
            if summary['status']=='PASS':continue
            if summary['stage'].startswith('awaiting_'):return {'state':summary['stage'],'scenario_id':scenario}
        else:
            if not case_source(root,scenario).exists():
                close_campaign(root,'blocked','ARCHIVED_E1_UNAVAILABLE');return {'state':'blocked','scenario_id':scenario,'reason':'ARCHIVED_E1_UNAVAILABLE'}
            try:summary=run_report(root,scenario,'live',provider_data) if scenario in REPORT_CASES else run_adversarial(root,scenario,provider_data)
            except Exception as error:
                code=getattr(error,'error_code',str(error) if str(error).isupper() else type(error).__name__)
                status='BLOCKED' if code in ENV_ERRORS else 'FAIL'
                summary={'scenario_id':scenario,'case_type':'report_only' if scenario in REPORT_CASES else 'reviewer_adversarial',
                    'status':status,'fresh_live_status':status,'recorded_response_replay_status':'NOT_RUN','stage':'finished',
                    'quality_rubric':None,'quality_rubric_applicable':scenario in REPORT_CASES,'reason_fa':code,'error_code':code}
                dump(path,summary)
            if summary['stage'].startswith('awaiting_'):return {'state':summary['stage'],'scenario_id':scenario}
        if summary['status'] in {'FAIL','BLOCKED'}:
            close_campaign(root,'failed' if summary['status']=='FAIL' else 'blocked',scenario+': '+summary['reason_fa']);return {'state':'failed' if summary['status']=='FAIL' else 'blocked','scenario_id':scenario}
    close_campaign(root,'complete',None);return {'state':'complete'}


def verify(root):
    """Pure read: an audit cannot rewrite statuses, counts, or dispatch history."""
    errors=[]
    try:
        ledger=read(root/'campaign_ledger.json');rows=ledger['attempts']
        if ledger['campaign_id']!=read(root/'run_manifest.json')['campaign_id']:errors.append('CAMPAIGN_ID_MISMATCH')
        if len(rows)>22 or len({r['invocation_id'] for r in rows})!=len(rows):errors.append('LEDGER_CAP_OR_DUPLICATE')
        if ledger['fresh_objective_evaluations']!=0 or ledger['solver_calls']!=0:errors.append('NUMERICAL_ACTIVITY')
        if ledger.get('objective_call_guard_violations',0) or ledger.get('solver_call_guard_violations',0):errors.append('NUMERICAL_GUARD_VIOLATION')
        if ledger['external_dispatches']!=sum(r.get('dispatched_at') is not None for r in rows):errors.append('DISPATCH_COUNT_MISMATCH')
        if any(r.get('status') not in {'reserved','succeeded','failed','outcome_unknown'} for r in rows):errors.append('INVALID_ATTEMPT_STATUS')
        sequence=[ORDER.index(r['scenario_id']) for r in rows if r['scenario_id'] in ORDER]
        if sequence!=sorted(sequence):errors.append('SCENARIO_EXECUTION_ORDER')
        for case in ORDER:
            selected=[r for r in rows if r['scenario_id']==case]
            if len(selected)>CAPS[case]:errors.append('SCENARIO_CAP:'+case)
            for role in ('explainer','report_reviewer'):
                if sum(r['role']==role for r in selected)>(2 if case in REPORT_CASES else 0 if role=='explainer' else 1):errors.append('ROLE_CAP:'+case)
        if any(r['scenario_id'] not in ORDER for r in rows):errors.append('UNKNOWN_SCENARIO')
        with (root/'call_usage.csv').open(newline='',encoding='utf-8') as stream:csv_rows=list(csv.DictReader(stream))
        if [r['invocation_id'] for r in csv_rows]!=[r['invocation_id'] for r in rows]:errors.append('CSV_LEDGER_MISMATCH')
        if len(csv_rows)==len(rows):
            for actual,recorded in zip(csv_rows,rows):
                if any(actual[k] != ('' if recorded.get(k) is None else str(recorded[k])) for k in USAGE_COLUMNS):errors.append('CSV_VALUE_MISMATCH')
        frozen=None
        if (root/'freeze.json').exists():frozen=validate_freeze(root)
        summaries=[]
        for case in ORDER:
            path=root/'live'/case/'summary.json'
            if path.exists():
                result=read(path);summaries.append(result)
                if result['scenario_id']!=case or result['status'] not in STATUSES:errors.append('INVALID_SCENARIO_RESULT')
                expected_type='report_only' if case in REPORT_CASES else 'reviewer_adversarial'
                if result.get('case_type')!=expected_type:errors.append('INVALID_SCENARIO_CASE_TYPE:'+case)
                if result['status']=='PASS' and result['case_type']=='report_only':
                    r=read(path.parent/'report.json');n=r['narrative_v3'];accepted=n['accepted_sections'];ids=[s['section']['section_id'] for s in accepted]
                    rubric=result['quality_rubric'];paragraphs={p['paragraph_id'] for s in n['rendered_sections'] for p in s['paragraphs']}
                    required_counts={s:sum(x['section']['section_id']==s and x['origin']=='llm' for x in accepted) for s in REQUIRED_SECTIONS}
                    if (any(c!=1 for c in required_counts.values()) or len(ids)!=len(set(ids)) or n['mode']!='llm' or n['review_status']!='passed'
                        or len(rubric)!=6 or {q['id'] for q in rubric}!={f'Q{i}' for i in range(1,7)}
                        or any(q['status']!='PASS' or not q.get('paragraph_ids') or not set(q['paragraph_ids'])<=paragraphs for q in rubric)):
                        errors.append('FALSE_REPORT_PASS:'+case)
                    if result['report_sha256']!=file_hash(path.parent/'report.json'):errors.append('REPORT_FILE_HASH_CHANGED:'+case)
                    if core_hash(r)!=read(root/'fixtures_manifest.json')['cases'][case]['source_core_sha256']:errors.append('REPORT_CORE_CHANGED:'+case)
                if result['status']=='PASS' and result['case_type']=='reviewer_adversarial':
                    prepared=read(root/'prepared'/f'{case}.json');response=read(path.parent/'response.json')
                    checked=validate_review(prepared['reviewer_context'],response['output']);verdicts={r.section_id:r.verdict for r in checked.section_reviews}
                    if (not prepared['expected_accept'] or result['expected_accept']!=prepared['expected_accept']
                        or result['expected_reject']!=prepared['expected_reject'] or result['actual_verdicts']!=verdicts
                        or any(verdicts.get(s)!='accept' for s in prepared['expected_accept'])
                        or any(verdicts.get(s)!='reject' for s in prepared['expected_reject'])
                        or result.get('target_cause_audit',{}).get('status')!='PASS'):
                        errors.append('FALSE_ADVERSARIAL_PASS:'+case)
        if (root/'summary.json').exists():
            overall=read(root/'summary.json');scenarios=overall.get('scenarios',[])
            if len(scenarios)!=7 or {s['scenario_id'] for s in scenarios}!=set(ORDER):errors.append('SUMMARY_SCENARIO_SET')
            for s in scenarios:
                if s.get('status') not in STATUSES or s.get('fresh_live_status','NOT_RUN') not in STATUSES:errors.append('INVALID_OVERALL_SCENARIO_STATUS')
                if s.get('case_type')!=('report_only' if s['scenario_id'] in REPORT_CASES else 'reviewer_adversarial'):
                    errors.append('INVALID_OVERALL_CASE_TYPE:'+s['scenario_id'])
                path=root/'live'/s['scenario_id']/'summary.json'
                if path.exists() and read(path)['status']!=s.get('status'):errors.append('OVERALL_SCENARIO_STATUS_MISMATCH:'+s['scenario_id'])
                if not path.exists() and s.get('status')=='PASS':errors.append('MISSING_LIVE_PASS:'+s['scenario_id'])
            actual=overall.get('actual',{})
            if actual.get('charged_role_attempts')!=len(rows) or actual.get('external_dispatches')!=ledger['external_dispatches']:errors.append('OVERALL_CONSUMPTION_MISMATCH')
        if (root/'acceptance_matrix.csv').exists():
            with (root/'acceptance_matrix.csv').open(newline='',encoding='utf-8') as stream:matrix=list(csv.DictReader(stream))
            if len(matrix)!=34 or {r['requirement_id'] for r in matrix}!={f'FC-{i:02d}' for i in range(1,35)}:errors.append('MATRIX_REQUIREMENT_SET')
            for row in matrix:
                if row['status'] not in STATUSES:errors.append('INVALID_MATRIX_STATUS')
                paths=json.loads(row['evidence_paths'])
                safe_paths=isinstance(paths,list) and all(isinstance(p,str) and p and not Path(p).is_absolute()
                    and '..' not in Path(p).parts and (root/Path(p)).resolve().is_relative_to(root.resolve()) for p in paths)
                if not safe_paths:errors.append('UNSAFE_EVIDENCE_PATH:'+row['requirement_id'])
                if row['status']=='PASS':
                    if not safe_paths or not paths or not all((root/p).is_file() for p in paths):errors.append('MISSING_PASS_EVIDENCE:'+row['requirement_id'])
                    live_gates={'FC-21':'C-L1','FC-22':'C-L2','FC-23':'C-L3','FC-24':'C-L4',
                        'FC-25':'C-A1','FC-26':'C-A2','FC-27':'C-E1-R'}
                    if row['requirement_id'] in live_gates:
                        case=live_gates[row['requirement_id']]
                        if not any(s.get('scenario_id')==case and s.get('status')=='PASS' for s in summaries):
                            errors.append('LIVE_GATE_RESULT_MISMATCH:'+row['requirement_id'])
                    if row['requirement_id']=='FC-20' and frozen is None:errors.append('MISSING_FROZEN_SOURCE_FOR_ACCEPTANCE')
            if (root/'summary.json').exists() and len(matrix)==34 and {r['requirement_id'] for r in matrix}=={f'FC-{i:02d}' for i in range(1,35)}:
                def aggregate(selected):
                    states=[r['status'] for r in selected]
                    return 'FAIL' if 'FAIL' in states else 'BLOCKED' if not states or any(s!='PASS' for s in states) else 'PASS'
                candidate=aggregate([r for r in matrix if r['requirement_id']!='FC-33'])
                closure=aggregate(matrix)
                if overall.get('candidate_acceptance_status')!=candidate:errors.append('CANDIDATE_AGGREGATE_MISMATCH')
                if overall.get('narrative_closure_status')!=closure:errors.append('CLOSURE_AGGREGATE_MISMATCH')
                if overall.get('delivery_preparation_readiness')!=('READY' if closure=='PASS' else 'NOT_READY'):errors.append('READINESS_AGGREGATE_MISMATCH')
                if overall.get('packaging_validation')!='NOT_RUN' or overall.get('professor_machine_installation')!='NOT_RUN':errors.append('UNSUPPORTED_DELIVERY_CLAIM')
        elif (root/'summary.json').exists():errors.append('MISSING_ACCEPTANCE_MATRIX')
        return {'status':'FAIL' if errors else 'PASS','verification_scope':'integrity/accounting only; never upgrades feature acceptance','errors':errors,
            'charged_role_attempts':len(rows),'external_dispatches':ledger['external_dispatches'],'fresh_objective_evaluations':ledger['fresh_objective_evaluations'],
            'solver_calls':ledger['solver_calls'],'frozen':frozen is not None,'scenarios_with_results':len(summaries)}
    except Exception as error:return {'status':'FAIL','errors':[type(error).__name__+': '+str(error)[:180]]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',required=True,choices=['offline','live','verify']);parser.add_argument('--fixture-root',type=Path,required=True)
    parser.add_argument('--archived-e1',type=Path,required=True);parser.add_argument('--evidence-root',type=Path,required=True)
    parser.add_argument('--provider-data-dir',type=Path);parser.add_argument('--resume',action='store_true')
    args=parser.parse_args();root=args.evidence_root.resolve();provider_data=data_directory(args.provider_data_dir)
    if args.mode=='verify':result=verify(root)
    elif args.mode=='offline':
        with no_numerics():result=prepare(root,args.fixture_root.resolve(),args.archived_e1.resolve(),args.resume)
    else:
        if not args.resume:raise RuntimeError('LIVE_REQUIRES_RESUME_OF_PREPARED_FROZEN_CAMPAIGN')
        try:
            validate_inputs(root,args.fixture_root.resolve(),args.archived_e1.resolve());result=run_live(root,provider_data)
        except Exception as error:
            code=getattr(error,'error_code',str(error) if str(error).isupper() else type(error).__name__)
            state='blocked' if code in ENV_ERRORS else 'failed'
            close_campaign(root,state,code);result={'state':state,'error_code':code}
            dump(root/'live/campaign-blocker.json',result);write_usage(root)
    output=result
    if args.mode=='offline':output={'status':result['status'],'runtime_manifest_sha256':result['runtime']['canonical_source_sha256'],
        'harness_manifest_sha256':result['harness']['canonical_harness_sha256'],'prepared_cases':list(result['prepared_cases']),
        'fresh_objective_evaluations':0,'solver_calls':0,'external_dispatches':0}
    print(json.dumps(output,ensure_ascii=False));return 1 if result.get('status')=='FAIL' or result.get('state') in {'failed','blocked'} else 0

if __name__=='__main__':raise SystemExit(main())
