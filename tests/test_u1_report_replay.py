"""Isolated report replay controls; all provider responses here are local mocks."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).parents[1]


def module():
    spec = importlib.util.spec_from_file_location('u1_report_replay', ROOT / 'scripts/qa_u1_report_replay.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_offline_replay_publishes_fresh_journal_preserves_core_and_refuses_repeat(tmp_path, monkeypatch):
    m = module()
    source = json.loads((ROOT / 'tests/fixtures/narrative_v3/F1.json').read_text(encoding='utf-8'))
    source['report_generation_policy_version'] = '1.2'
    source['execution_contracts'] = {'explanation': 'grounded_narrative_v3',
        'report_generation_policy_version': '1.2', 'report_generation_policy': m.freeze_narrative_policy('1.2')}
    source['numeric_execution']['authorization'] = {'scope': 'historical-numeric', 'accepted': True}
    source_path = tmp_path / 'source.json'
    source_path.write_text(json.dumps(source, ensure_ascii=False), encoding='utf-8')
    source_bytes = source_path.read_bytes()
    before = m.core_hash(source)
    # Other agents may be editing unrelated files during the targeted unit run.
    frozen = m.source_freeze(require_committed=False)
    monkeypatch.setattr(m, 'source_freeze', lambda **kwargs: deepcopy(frozen))
    out, data = tmp_path / 'evidence', tmp_path / 'private'
    result = m.run_report_replay(source_path, out, data)
    assert result['status'] == 'PASS'
    assert result['mode'] == 'offline_mock'
    assert result['actual_model_submissions'] == 0
    assert result['role_attempts'] == 2
    assert result['fresh_objective_evaluations'] == result['solver_calls'] == result['connection_tests'] == 0
    assert result['guards'] == {'objective_calls': 0, 'solver_calls': 0}
    assert result['fresh_numerical_rows'] == {'trials': 0, 'stage_executions': 0}
    assert source_path.read_bytes() == (out / 'source_report.json').read_bytes() == source_bytes
    report = m.read(out / 'report.json')
    assert m.core_hash(report) == before
    assert report['numeric_execution']['authorization'] == source['numeric_execution']['authorization']
    assert report['qa_reporting']['schema_version'] == 'u1-report-replay/1.0'
    assert report['qa_reporting']['scope'] == 'report_only'
    assert report['report_generation_policy_version'] == '1.3'
    assert report['execution_contracts']['report_generation_policy_version'] == '1.3'
    assert m.read(out / 'source_report.json')['report_generation_policy_version'] == '1.2'
    assert report['source_run_timing'] == source['timing']
    assert report['timing']['elapsed_compute_seconds'] == source['timing']['elapsed_compute_seconds']
    assert (out / 'report.html').read_bytes() == (out / 'reopened.html').read_bytes()
    journal = m.read(out / 'journal.json')
    assert journal['report_generation_policy_version'] == '1.3'
    assert len(m.read(out / 'invocations.json')) == 2
    repo = m.Repository(data)
    assert repo.get_job(source['job_id'])['job_status'] == 'completed'
    assert repo.get_report(source['job_id'])['report'] == report
    immutable_files = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
    with pytest.raises(RuntimeError, match='ALREADY_STARTED'):
        m.run_report_replay(source_path, out, data)
    assert {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()} == immutable_files


def test_ledger_reserves_four_including_unknown_outcome_and_role_caps(tmp_path):
    m = module()
    path = tmp_path / 'ledger.json'
    m.new_ledger(path, live=True)
    context = {'request_hash': 'a' * 64}
    for index, role in enumerate(('explainer', 'report_reviewer', 'explainer', 'report_reviewer')):
        m.reserve_attempt(path, role, m.ROLES[role], str(index), context, 'repair')
        m.update_attempt(path, str(index), status='outcome_unknown')
    assert len(m.read(path)['attempts']) == 4
    with pytest.raises(RuntimeError, match='BUDGET_EXHAUSTED'):
        m.reserve_attempt(path, 'report_reviewer', m.ROLES['report_reviewer'], 'extra', context, 'review')
    with pytest.raises(RuntimeError, match='INVOCATION_REUSE'):
        m.reserve_attempt(path, 'explainer', m.ROLES['explainer'], '0', context, 'draft')
    with pytest.raises(RuntimeError, match='ALREADY_STARTED'):
        m.new_ledger(path, live=True)
    assert len(module().read(path)['attempts']) == 4


def transport_fixture(tmp_path):
    m = module()
    path = tmp_path / 'ledger.json'
    m.new_ledger(path, live=True)
    context = {'request_hash': 'a' * 64}
    invocation = 'fixture-invocation'
    m.reserve_attempt(path, 'explainer', 'explainer.v3', invocation, context, 'draft')
    body = b'{"model":"TEST_FIXTURE","max_completion_tokens":8192}'
    data = {'context': context, 'invocation_id': invocation, 'schema_id': 'explainer.v3'}
    class Repo:
        row = {'call_id': 'fixture-call', 'role': 'explainer', 'profile_revision': 1,
               'request_hash': m.value_hash(data), 'input': json.dumps(data),
               'metadata': json.dumps({'provenance': {'request_body_sha256': sha256(body).hexdigest(),
                   'report_generation_policy_version': '1.3',
                   'report_generation_policy_hash': m.freeze_narrative_policy('1.3')['report_generation_policy_hash']}})}
        @contextmanager
        def connect(self):
            yield self
        def execute(self, *args):
            return self
        def fetchone(self):
            return self.row
    repo = Repo()
    transport = m.ReplayTransport(repo, path, lambda: None)
    transport.inner.close()
    sent = []
    transport.inner = httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200, json={'ok': True}))
    transport.invocation = invocation
    return m, path, repo, transport, httpx.Request('POST', 'https://provider.invalid/v1/chat/completions', content=body), sent


def test_dispatch_is_charged_before_send_and_hidden_retry_is_blocked(tmp_path):
    m, path, repo, transport, request, sent = transport_fixture(tmp_path)
    try:
        assert transport.handle_request(request).status_code == 200
        assert len(sent) == m.read(path)['external_dispatches'] == 1
        with pytest.raises(RuntimeError, match='HIDDEN_RETRY'):
            transport.handle_request(request)
        assert len(sent) == 1
        assert m.read(path)['attempts'][0]['request_body_sha256'] == sha256(request.content).hexdigest()
    finally:
        transport.finish()


@pytest.mark.parametrize('defect', ['missing_reservation', 'body_changed', 'request_changed', 'policy_changed', 'freeze_changed'])
def test_invalid_dispatch_never_reaches_provider(tmp_path, defect):
    m, path, repo, transport, request, sent = transport_fixture(tmp_path)
    if defect == 'missing_reservation':
        repo.row = None
    elif defect == 'body_changed':
        request = httpx.Request('POST', str(request.url), content=b'{}')
    elif defect == 'request_changed':
        repo.row['request_hash'] = 'b' * 64
    elif defect == 'policy_changed':
        meta = json.loads(repo.row['metadata'])
        meta['provenance']['report_generation_policy_version'] = '1.2'
        repo.row['metadata'] = json.dumps(meta)
    else:
        def changed():
            raise RuntimeError('SOURCE_CHANGED')
        transport.check_freeze = changed
    try:
        with pytest.raises(RuntimeError):
            transport.handle_request(request)
        assert not sent
        assert m.read(path)['external_dispatches'] == 0
    finally:
        transport.finish()


@pytest.mark.parametrize('location', ['evidence', 'source_profile'])
def test_private_store_cannot_overwrite_source_or_enter_public_evidence(tmp_path, location):
    m = module()
    source_profile = tmp_path / 'source-profile'
    out = tmp_path / 'evidence'
    private = out / 'private' if location == 'evidence' else source_profile
    with pytest.raises(RuntimeError, match='PRIVATE_DATA|SOURCE_DATA'):
        m.run_report_replay(tmp_path / 'absent.json', out, private, source_profile_data=source_profile, live=True)
    assert not out.exists() and not source_profile.exists()


def test_committed_source_gate_rejects_dirty_live_source(monkeypatch):
    m = module()
    monkeypatch.setattr(m, 'git', lambda *args: ' M src/p_mc_optimizer/changed.py')
    with pytest.raises(RuntimeError, match='SOURCE_NOT_COMMITTED'):
        m.source_freeze(require_committed=True)
