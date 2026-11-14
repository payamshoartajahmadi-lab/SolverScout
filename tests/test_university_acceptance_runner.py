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


def test_saved_report_banner_handles_the_actual_renderer_body_attributes():
    import re
    m = module()
    report = json.loads((Path(__file__).parent / 'fixtures/narrative_v3/F1.json').read_text())
    original = deepcopy(report)
    actual = m.html_bytes(report).decode('utf-8')
    assert re.search(r'<main\b', actual), 'Exercise the actual shared renderer output'
    rendered = m.stored_report_html(report)
    assert rendered.count('data-delivery-saved-report="true"') == 1
    assert re.search(r'(?:<body\b[^>]*>\s*)?<aside data-delivery-saved-report', rendered)
    assert re.search(r'<title\b[^>]*>گزارش ذخیره‌شده', rendered)
    assert report == original
    variant = actual.replace('<main>', '<body class="report" dir="rtl"><main>', 1)
    assert '<body class="report" dir="rtl">' in variant
    explicit = m.label_saved_report_html(variant, report['job_id'])
    assert re.search(r'<body\b[^>]*>\s*<aside data-delivery-saved-report', explicit)
    assert explicit.count('data-delivery-saved-report="true"') == 1


def zero_fe_retry_fixture(m):
    """Controlled copy of the observed failure shape, not a model-quality test."""
    text = (m.ROOT / 'examples/university_u2_fa.txt').read_text()
    request = m.request_for(fake_app(m), 'U2', text); frozen = manifest(m)
    binding = {'campaign_id': frozen['campaign_id'], 'job_id': 'original-failed-job',
               'scenario': 'U2', 'request_hash': m.value_hash(request.model_dump(mode='json'))}
    job = {'job_id': binding['job_id'], 'job_status': 'failed', 'input': request.input.model_dump(mode='json'),
           'error': {'error_code': 'UNSUPPORTED_PROBLEM'}, 'budget': {'known_started': 0, 'charged_unknown': 0, 'reserved_active': 0}}
    row = {'call_id': 'original-formulator-call', 'role': 'formulator', 'status': 'succeeded',
           'input': {'context': {'sources': [{'source_ref': 'input:text', 'text': text}]}},
           'output': {'unsupported_features': [{'kind': 'solver_selection_and_execution'}],
                      'extracted_fields': {'objective': {'expression': '(1-x1)**2 + 100*(x2-x1**2)**2', 'sense': 'minimize', 'source_kind': 'expression'},
                                           'variables': [{'name': 'x1', 'type': 'real', 'lower': -2, 'upper': 2}, {'name': 'x2', 'type': 'real', 'lower': -1, 'upper': 3}],
                                           'deterministic': True, 'parameters': [], 'constraints': []}}}
    ledger = {'stop_reason': None, 'attempts': [{'scenario_id': 'U2', 'role': 'formulator', 'status': 'succeeded', 'call_id': row['call_id']}]}
    return request, frozen, binding, job, row, ledger


def test_only_the_exact_zero_fe_workflow_intent_failure_is_retry_eligible():
    m = module(); req, frozen, binding, job, row, ledger = zero_fe_retry_fixture(m)
    untouched = deepcopy((job, row, ledger, binding))
    m.validate_preflight_retry(job, [row], req, frozen, binding, 'U2', ledger)
    assert (job, row, ledger, binding) == untouched


@pytest.mark.parametrize('defect', ['spent_fe', 'unknown_fe', 'pending_fe', 'unknown_response', 'other_unsupported_kind',
                                  'input_hash_changed', 'request_changed', 'invalid_math', 'third_attempt', 'wrong_scenario'])
def test_preflight_retry_rejects_every_out_of_scope_or_unsafe_case(defect):
    m = module(); req, frozen, binding, job, row, ledger = zero_fe_retry_fixture(m); scenario = 'U2'
    if defect == 'spent_fe': job['budget']['known_started'] = 1
    elif defect == 'unknown_fe': job['budget']['charged_unknown'] = 1
    elif defect == 'pending_fe': job['budget']['reserved_active'] = 1
    elif defect == 'unknown_response': row['status'] = 'outcome_unknown'
    elif defect == 'other_unsupported_kind': row['output']['unsupported_features'][0]['kind'] = 'integer_variable'
    elif defect == 'input_hash_changed': frozen['scenarios']['U2']['input_sha256'] = 'b' * 64
    elif defect == 'request_changed': binding['request_hash'] = 'b' * 64
    elif defect == 'invalid_math': row['output']['extracted_fields']['variables'][0]['type'] = 'integer'
    elif defect == 'third_attempt': binding['attempt_number'] = 2
    else: scenario = 'U1'
    with pytest.raises(RuntimeError, match='PREFLIGHT_RETRY'):
        m.validate_preflight_retry(job, [row], req, frozen, binding, scenario, ledger)


def test_single_retry_archives_old_failure_and_keeps_cumulative_ledger(tmp_path):
    m = module(); req, frozen, binding, old_job, row, state = zero_fe_retry_fixture(m)
    directory = tmp_path / 'private'; directory.mkdir()
    binding_path = directory / 'U2.binding.json'; m.dump(binding_path, binding); original_binding = binding_path.read_bytes()
    ledger = directory / 'campaign_ledger.json'; m.dump(ledger, state); original_ledger = ledger.read_bytes()
    out = tmp_path / 'public/live/U2'; out.mkdir(parents=True)
    m.dump(out / 'report.json', {'job_id': old_job['job_id'], 'job_status': 'failed'})
    m.dump(out / 'human-quality-audit.json', {'status': 'NOT_RUN', 'job_id': old_job['job_id']})
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    class Service:
        count = 0
        jobs = {old_job['job_id']: old_job}
        @contextmanager
        def connect(self): yield self
        def execute(self, *args): return self
        def fetchall(self): return [{**row, 'input': json.dumps(row['input']), 'output': json.dumps(row['output'])}]
        def __iter__(self): return iter(self.fetchall())
        def get_job(self, identity): return self.jobs[identity]
        def list_trials(self, identity): return []
        def list_executions(self, identity): return []
        def create_job(self, request, key):
            self.count += 1; assert request.model_dump(mode='json') == req.model_dump(mode='json')
            self.jobs['retry-job'] = {'job_id': 'retry-job', 'job_status': 'queued'}
            return self.jobs['retry-job']
    service = Service()
    new = m.retry_preflight_job(service, service, req, directory, frozen, 'U2', tmp_path / 'public', ledger)
    assert new['job_id'] == 'retry-job' and service.count == 1
    assert ledger.read_bytes() == original_ledger
    archive = out / 'attempt-01-preflight-failure'
    assert all((archive / name).read_bytes() == value for name, value in before.items())
    assert (archive / 'original-U2.binding.json').read_bytes() == original_binding
    assert m.read(binding_path)['prior_job_ids'] == [old_job['job_id']]
    assert m.retry_preflight_job(service, service, req, directory, frozen, 'U2', tmp_path / 'public', ledger)['job_id'] == 'retry-job'
    assert service.count == 1
    service.jobs['retry-job']['job_status'] = 'failed'
    with pytest.raises(RuntimeError, match='ALREADY_USED'):
        m.retry_preflight_job(service, service, req, directory, frozen, 'U2', tmp_path / 'public', ledger)
    assert service.count == 1 and ledger.read_bytes() == original_ledger
