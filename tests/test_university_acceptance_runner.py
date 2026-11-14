"""Offline controls for the thin university runner; no provider or solver runs."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest


def module():
    spec = importlib.util.spec_from_file_location('university_acceptance_runner', Path(__file__).parents[1] / 'scripts/qa_university_delivery.py')
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result); return result


def manifest(m):
    return {'campaign_id': 'TEST_FIXTURE_UNIVERSITY_CAMPAIGN', 'limit_role_calls': 20,
            'scenario_limits': {'U1': 8, 'U2': 8, 'U-REPORT': 4}, 'objective_evaluations_limit': 40000,
            'source_manifest_sha256': m.value_hash(m.source_manifest()),
            'prompt_bundle_sha256': m.value_hash(m.prompt_bundle_manifest()), 'settings': deepcopy(m.SETTINGS),
            'scenarios': {key: {**{k: v[k] for k in ('threshold', 'retrieval_mode')},
                                'input_sha256': m.file_hash(m.ROOT / 'examples' / v['file'])} for key, v in m.CASES.items()}}


def test_manifest_rejects_changed_threshold_input_and_caps_before_dispatch(tmp_path):
    m = module(); frozen = manifest(m)
    m.dump(tmp_path / 'acceptance_manifest.json', frozen)
    assert m.validate_manifest(tmp_path) == frozen
    for field in ('threshold', 'input_sha256'):
        changed = deepcopy(frozen); changed['scenarios']['U1'][field] = 0
        m.dump(tmp_path / 'acceptance_manifest.json', changed)
        with pytest.raises(RuntimeError, match='INPUT_OR_CRITERIA'): m.validate_manifest(tmp_path)
    changed = deepcopy(frozen); changed['limit_role_calls'] = 21
    m.dump(tmp_path / 'acceptance_manifest.json', changed)
    with pytest.raises(RuntimeError, match='LIMITS_MISMATCH'): m.validate_manifest(tmp_path)


def test_existing_campaign_ledger_caps_survive_process_style_reopening(tmp_path):
    m = module(); frozen = manifest(m); path = m.prepare_ledger(tmp_path, frozen)
    for scenario, limit in [('U1', 8), ('U2', 8), ('U-REPORT', 4)]:
        for index in range(limit):
            assert m.reserve_campaign_call(path, scenario, scenario + str(index), 'a' * 64, 'report_reviewer', 'narrative_review.v1')
    reloaded = module(); assert reloaded.prepare_ledger(tmp_path, frozen) == path
    assert len(m.read(path)['attempts']) == 20
    assert m.reserve_campaign_call(path, 'U1', 'U10', 'a' * 64, 'report_reviewer', 'narrative_review.v1') is False
    with pytest.raises(RuntimeError, match='BUDGET_EXHAUSTED'):
        m.reserve_campaign_call(path, 'U-REPORT', 'extra', 'a' * 64, 'report_reviewer', 'narrative_review.v1')
    changed = deepcopy(frozen); changed['campaign_id'] = 'unapproved-campaign-reset'
    with pytest.raises(RuntimeError, match='LEDGER_BINDING'): m.prepare_ledger(tmp_path, changed)


def transport_fixture(tmp_path):
    m = module(); path = m.prepare_ledger(tmp_path, manifest(m))
    body = b'{"model":"TEST_FIXTURE","max_completion_tokens":4096}'
    class Repo:
        row = {'call_id': 'durable-fixture-call', 'status': 'reserved', 'request_hash': 'a' * 64,
               'role': 'report_reviewer', 'profile_revision': 3,
               'metadata': json.dumps({'provenance': {'request_body_sha256': sha256(body).hexdigest(), 'effective_max_output_tokens': 4096}})}
        @contextmanager
        def connect(self): yield self
        def execute(self, *args): return self
        def fetchone(self): return self.row
    repo = Repo(); transport = m.CampaignTransport(repo, path, 'U1'); transport.inner.close(); calls = []
    transport.inner = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json={'ok': True}))
    transport.active.invocation = SimpleNamespace(invocation_id='fixture-invocation', job_id='fixture-job', role='report_reviewer', schema_id='narrative_review.v1')
    request = httpx.Request('POST', 'https://provider.invalid/v1/chat/completions', content=body)
    return m, repo, transport, request, calls, path


def test_dispatch_checks_durable_body_charges_once_and_blocks_hidden_retry(tmp_path):
    m, repo, transport, request, calls, path = transport_fixture(tmp_path)
    try:
        assert transport.handle_request(request).status_code == 200
        assert len(calls) == len(m.read(path)['attempts']) == 1
        with pytest.raises(RuntimeError, match='HIDDEN_EXTERNAL_RETRY'): transport.handle_request(request)
        assert len(calls) == 1
        state = m.read(path)
        assert state['stop_reason'] == 'HIDDEN_EXTERNAL_RETRY'
        assert state['attempts'][0]['request_body_sha256'] == sha256(request.content).hexdigest()
    finally: transport.finish()


@pytest.mark.parametrize('defect', ['missing_reservation', 'body_mismatch'])
def test_unbound_or_changed_request_cannot_dispatch_or_consume_api_attempt(tmp_path, defect):
    m, repo, transport, request, calls, path = transport_fixture(tmp_path)
    if defect == 'missing_reservation': repo.row = None
    else: repo.row['metadata'] = json.dumps({'provenance': {'request_body_sha256': 'b' * 64}})
    try:
        with pytest.raises(RuntimeError): transport.handle_request(request)
        assert not calls and not m.read(path)['attempts']
    finally: transport.finish()


def fake_app(m):
    return SimpleNamespace(state=SimpleNamespace(
        gateway=SimpleNamespace(get_profile=lambda: {'profile': {'profile_id': str(uuid4()), 'revision': 1, 'test_status': 'passed'}, 'credential_status': {'available': True}}),
        preparation=SimpleNamespace(status=lambda: {'ready': True}),
        catalog=SimpleNamespace(snapshot=lambda: SimpleNamespace(cards=[SimpleNamespace(implementation_id=i) for i in m.TARGETS], catalog_hash='a' * 64)),
        search_settings=SimpleNamespace(get_sources=lambda: SimpleNamespace(revision=1)),
        library=SimpleNamespace(list_sources=lambda: {'sources': [{'rule': {'source_id': 'mealpy_docs'}, 'config': {'revision': 1}}]})))


@pytest.mark.parametrize('scenario', ['U1', 'U2'])
def test_real_createjob_contract_has_exact_text_and_fixed_policy_without_api(scenario):
    m = module(); text = (m.ROOT / 'examples' / m.CASES[scenario]['file']).read_text()
    request = m.request_for(fake_app(m), scenario, text)
    assert request.input.kind == 'natural_language' and request.input.text == text
    assert request.policy.master_seed == 1405 and request.policy.max_objective_evaluations == 20000
    assert request.policy.max_compute_seconds == 300
    assert request.policy.retrieval_mode == m.CASES[scenario]['retrieval_mode']
    assert bool(request.source_config_refs) == (scenario == 'U1')


def test_scenario_binding_reuses_same_job_and_rejects_changed_request(tmp_path):
    m = module(); request = m.request_for(fake_app(m), 'U1', (m.ROOT / 'examples/university_u1_fa.txt').read_text())
    class Service:
        count = 0
        def create_job(self, req, key): self.count += 1; return {'job_id': 'same-job'}
        def get_job(self, identity): return {'job_id': identity, 'job_status': 'completed'}
    service = Service(); frozen = manifest(m)
    assert m.bind_job(service, service, request, tmp_path, frozen, 'U1')['job_id'] == 'same-job'
    assert m.bind_job(service, service, request, tmp_path, frozen, 'U1')['job_id'] == 'same-job'
    assert service.count == 1
    changed = request.model_copy(update={'catalog_hash': 'b' * 64})
    with pytest.raises(RuntimeError, match='BINDING_CHANGED'): m.bind_job(service, service, changed, tmp_path, frozen, 'U1')


def test_no_live_flag_stops_before_datastore_or_transport(tmp_path, monkeypatch):
    m = module()
    monkeypatch.setattr(m, 'validate_manifest', lambda _: pytest.fail('No explicit live authorization'))
    with pytest.raises(RuntimeError, match='EXPLICIT_LIVE_FLAG_REQUIRED'):
        m.run(tmp_path, tmp_path, 'U1', tmp_path, live=False)


def test_actual_invocation_serialization_unwraps_advisor_context_for_all_checks():
    from p_mc_optimizer.llm.models import Invocation
    m = module()
    report = json.loads((Path(__file__).parent / 'fixtures/narrative_v3/F1.json').read_text())
    report['research'].update(query_plan_target_coverage={'targeted_implementation_ids': list(m.TARGETS)})
    report['narrative_v3'] = {'mode': 'llm', 'accepted_sections': [
        {'section': {'section_id': sid}, 'origin': 'llm', 'reviewer_invocation_id': 'CONTROLLED_FIXTURE'} for sid in m.REQUIRED_SECTIONS]}
    context = {'solver_cards': [{'implementation_id': identity} for identity in m.TARGETS],
               'evidence': [{'text': '## ' + '.'.join(identity.split(':')) + '\n\nDescription of this specific method.'} for identity in m.TARGETS]}
    invocation = Invocation.model_validate({'job_id': str(uuid4()), 'invocation_id': str(uuid4()),
        'role': 'advisor', 'schema_id': 'advisor.v2.1', 'context': context})
    # This is the production DTO serialization used by LLMGateway._reserve(data=...).
    row = {'role': 'advisor', 'input': invocation.model_dump(mode='json')}
    audit = m.independent_checks(report, [row], 'U1')
    assert audit['advisor_sent_count'] == 6 and set(audit['advisor_sent_implementation_ids']) == set(m.TARGETS)
    assert all(audit['checks'].values()), audit
    changed = deepcopy(row); changed['input']['context']['solver_cards'] = context['solver_cards'][:3]
    assert not m.independent_checks(report, [changed], 'U1')['checks']['six_target_cards_really_sent']
