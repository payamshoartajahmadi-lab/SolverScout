"""Actual prepared HTTP settings and gateway policy binding, with no paid calls."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from p_mc_optimizer.llm.client import invoke_role
from p_mc_optimizer.llm.models import ProviderSnapshot
from p_mc_optimizer.llm.narrative_policy import freeze_narrative_policy, resolve_narrative_policy
from p_mc_optimizer.llm.provider import build_request, prepare_provider_request, prompt_bundle_manifest
from p_mc_optimizer.reporting.evidence_v3 import canonical_bytes, core_hash
from p_mc_optimizer.workflow.narrative_v3 import NarrativeOrchestrator
from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, checked, completion, golden
from test_llm_workflow import configure, local_api, submit
from test_narrative_v3_workflow import correct_draft, review_response


def snapshot():
    return ProviderSnapshot.model_validate({**_PROFILE_SETTINGS, "max_output_tokens": 1234,
                                           "profile_id": str(uuid4()), "revision": 1,
                                           "credential_ref": "fixture-reference", "provider_snapshot_hash": "a"*64})


@pytest.mark.parametrize("schema,version,expected", [
    ("explainer.v3", "1.1", 8192), ("narrative_review.v1", "1.1", 4096),
    ("explainer.v3", "1.2", 8192), ("narrative_review.v1", "1.2", 4096),
    ("explainer.v3", "1.0", 1234), ("narrative_review.v1", "1.0", 1234),
    ("formulator.v1", "1.1", 1234), ("connection_test.v1", "1.1", 512),
])
def test_resolver_wire_caps_without_snapshot_mutation(schema, version, expected):
    profile=snapshot();before=profile.model_dump(mode="json")
    body=build_request(profile,schema,{},report_generation_policy_version=version)
    assert body[profile.token_limit_field]==expected
    assert profile.model_dump(mode="json")==before
    assert body['model']==profile.model


def test_explicit_lower_user_cap_and_unknown_versions():
    p=snapshot()
    resolved=resolve_narrative_policy('explainer.v3',p,policy_version='1.1',max_output_tokens_override=2048)
    assert resolved.effective_max_output_tokens==2048
    assert resolved.policy_hash!=freeze_narrative_policy('1.1')['report_generation_policy_hash']
    with pytest.raises(ValueError):freeze_narrative_policy('9.9')
    with pytest.raises(ValueError):freeze_narrative_policy('1.1',max_output_tokens_override=True)


@pytest.mark.parametrize('version',['1.0','1.1','1.2'])
@pytest.mark.parametrize('mode',['json_schema','json_object','text_json'])
def test_real_gateway_frozen_policy_http_and_exact_replay(tmp_path,version,mode):
    sent=[];seen=[]
    def provider(request):
        body=json.loads(request.content)
        if body['messages'][1]['content']=='Return the connection check as JSON.':
            assert body['max_completion_tokens']==512
            return completion({'ok':True})
        context=json.loads(body['messages'][1]['content']);role='explainer' if 'mode' in context else 'report_reviewer'
        cap=(8192 if role=='explainer' else 4096) if version in {'1.1','1.2'} else 1234
        assert body['max_completion_tokens']==cap
        with repo.connect() as db:
            row=db.execute("SELECT metadata,status,provider_snapshot FROM llm_calls WHERE job_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1",(job_id,)).fetchone()
        metadata=json.loads(row['metadata']);frozen=json.loads(row['provider_snapshot'])
        assert row['status']=='reserved' and metadata['effective_max_output_tokens']==cap
        assert metadata['report_generation_policy_version']==version
        assert metadata['provenance']['request_body_sha256']==sha256(request.content).hexdigest()
        assert frozen['max_output_tokens']==1234 and frozen['revision']==profile['revision']
        expected_prompt=('explainer.v3.1' if role=='explainer' else 'narrative_review.v1.2' if version=='1.2' else 'narrative_review.v1.1')
        assert metadata['prompt_name']==expected_prompt if version in {'1.1','1.2'} else metadata['prompt_name'] in {'explainer.v3','narrative_review.v1'}
        sent.append(role)
        return completion(correct_draft(context) if role=='explainer' else review_response(context))
    with local_api(tmp_path,httpx.MockTransport(provider)) as (repo,client,address,token):
        profile,_=configure(client,{**_PROFILE_SETTINGS,'response_mode':mode,'max_output_tokens':1234},_TEST_KEY)
        job_id=submit(client,profile,golden()['original_text'])
        owner=str(uuid4());repo.claim_job(owner);repo.update_job(job_id,phase='reporting',worker_id=owner)
        contracts=deepcopy(repo.get_job(job_id)['workflow']['execution_contracts'])
        # This regression explicitly exercises the historical frozen 1.1 policy;
        # new jobs now default to the versioned 1.2 reviewer repair protocol.
        if version=='1.1':
            contracts.update(report_generation_policy_version='1.1', report_generation_policy=freeze_narrative_policy('1.1'),
                             evidence_builder_version='1.1', formatter_version='1.1')
            repo.put_workflow(job_id,'execution_contracts',contracts,worker_id=owner)
        if version=='1.0':
            contracts.update(evidence_builder_version='1.0',formatter_version='1.0')
            contracts.pop('report_generation_policy_version');contracts.pop('report_generation_policy')
            repo.put_workflow(job_id,'execution_contracts',contracts,worker_id=owner)
        report=json.loads((Path(__file__).parent/'fixtures/narrative_v3/F1.json').read_text())
        report=json.loads(json.dumps(report).replace(report['job_id'],job_id))
        initial_core=core_hash(report)
        def invoke(role,schema,context,invocation):
            seen.append((role,schema,deepcopy(context),invocation))
            return invoke_role(address,token,job_id,role,context,invocation_id=invocation,schema_id=schema,request_timeout_seconds=5)
        result=NarrativeOrchestrator(report,invoke=invoke,persist=lambda value:repo.put_workflow(job_id,'narrative_v3_journal',value,worker_id=owner),
            prompt_bundle_sha256=sha256(canonical_bytes(prompt_bundle_manifest())).hexdigest(),report_generation_policy_version=version).run()
        assert result['mode']=='llm',{'issues':result['issues'],'attempts':result['attempts'],'sent':sent}
        assert result['evidence_builder_version']==result['formatter_version']==('1.1' if version=='1.2' else version)
        assert core_hash(report)==initial_core and sent==['explainer','report_reviewer']
        role,schema,ctx,invocation=seen[-1]
        replay=invoke_role(address,token,job_id,role,ctx,invocation_id=invocation,schema_id=schema)
        assert replay['status']=='succeeded' and len(sent)==2
        with repo.transaction() as db:
            original=db.execute('SELECT llm_profile FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]
            changed_profile=json.loads(original);changed_profile['model']+='-changed'
            db.execute('UPDATE jobs SET llm_profile=? WHERE job_id=?',(json.dumps(changed_profile),job_id))
        drift=invoke_role(address,token,job_id,role,ctx,invocation_id=invocation,schema_id=schema)
        assert drift['call_id'] is None and drift['error']['error_code']=='IDEMPOTENCY_CONFLICT'
        with repo.transaction() as db:db.execute('UPDATE jobs SET llm_profile=? WHERE job_id=?',(original,job_id))
        assert len(sent)==2
        changed='1.0' if version=='1.1' else '1.1'
        contracts.update(report_generation_policy_version=changed,report_generation_policy=freeze_narrative_policy(changed),evidence_builder_version=changed,formatter_version=changed)
        repo.put_workflow(job_id,'execution_contracts',contracts,worker_id=owner)
        conflict=invoke_role(address,token,job_id,role,ctx,invocation_id=invocation,schema_id=schema)
        assert conflict['call_id'] is None and conflict['error']['error_code']=='IDEMPOTENCY_CONFLICT'
        assert len(sent)==2


@pytest.mark.parametrize('failure',['connection_proof','generation'])
def test_new_reporting_policy_cannot_bypass_existing_readiness(tmp_path,failure):
    external_roles=[]
    def provider(request):
        body=json.loads(request.content)
        if body['messages'][1]['content']=='Return the connection check as JSON.':return completion({'ok':True})
        external_roles.append(body);raise AssertionError('Not ready: no role request is allowed')
    with local_api(tmp_path,httpx.MockTransport(provider)) as (repo,client,address,token):
        profile,_=configure(client,_PROFILE_SETTINGS,_TEST_KEY);job_id=submit(client,profile,golden()['original_text'])
        owner=str(uuid4());repo.claim_job(owner);repo.update_job(job_id,phase='reporting',worker_id=owner)
        contracts=deepcopy(repo.get_job(job_id)['workflow']['execution_contracts'])
        contracts.update(report_generation_policy_version='1.1',report_generation_policy=freeze_narrative_policy('1.1'),
                         evidence_builder_version='1.1',formatter_version='1.1')
        repo.put_workflow(job_id,'execution_contracts',contracts,worker_id=owner)
        with repo.transaction() as db:
            if failure=='connection_proof':db.execute("DELETE FROM llm_calls WHERE purpose='connection_test'")
            else:db.execute('UPDATE credential_refs SET generation=generation+1')
        report=json.loads((Path(__file__).parent/'fixtures/narrative_v3/F1.json').read_text())
        report=json.loads(json.dumps(report).replace(report['job_id'],job_id))
        result=NarrativeOrchestrator(report,
            invoke=lambda role,schema,ctx,inv:invoke_role(address,token,job_id,role,ctx,invocation_id=inv,schema_id=schema),
            persist=lambda value:repo.put_workflow(job_id,'narrative_v3_journal',value,worker_id=owner),
            prompt_bundle_sha256=sha256(canonical_bytes(prompt_bundle_manifest())).hexdigest(),report_generation_policy_version='1.1').run()
        assert result['mode']=='deterministic' and not external_roles
        assert len(result['attempts'])==1 and result['attempts'][0]['call_id'] is None
        with repo.connect() as db:assert db.execute('SELECT COUNT(*) FROM llm_calls WHERE job_id=?',(job_id,)).fetchone()[0]==0
