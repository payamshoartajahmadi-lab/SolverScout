#!/usr/bin/env python3
"""One bounded U1 report regeneration from immutable, already computed evidence.

This creates a fresh private datastore and journal. It never resumes the old
university campaign, tests a live connection, prepares methods, or runs a solver.
Omit --live for a controlled offline provider fixture, never live acceptance.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic

import httpx
from filelock import FileLock

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / 'src', ROOT / 'scripts'):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from qa_narrative_v3 import copy_profile_tables, mock_provider, setup_offline_profile
from qa_narrative_v3_closure import dump, no_numerics
from p_mc_optimizer.launcher import instance_lock
from p_mc_optimizer.llm.gateway import LLMGateway
from p_mc_optimizer.llm.models import Invocation
from p_mc_optimizer.llm.narrative_models import REQUIRED_SECTIONS
from p_mc_optimizer.llm.narrative_policy import freeze_narrative_policy
from p_mc_optimizer.llm.provider import prompt_bundle_manifest
from p_mc_optimizer.llm.storage import job_call_metadata
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.persistence.repository import json_text, utc_now
from p_mc_optimizer.reporting import html_bytes
from p_mc_optimizer.reporting.evidence_v3 import core_hash, value_hash
from p_mc_optimizer.reporting.narrative_v3 import apply_narrative_v3
from p_mc_optimizer.runtime_provenance import get_runtime, source_manifest
from p_mc_optimizer.workflow.narrative_v3 import NarrativeOrchestrator

POLICY_VERSION = '1.3'
SOURCE_JOB_ID = 'cf73fe30-ef5a-4995-8fd7-4aa6256f3085'
ROLES = {'explainer': 'explainer.v3', 'report_reviewer': 'narrative_review.v1'}
TRACKED_PATHS = ('src', 'scripts', 'tests', 'schemas', 'pyproject.toml',
                 'requirements.lock', 'requirements-test.lock')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def now():
    return datetime.now(UTC).isoformat()


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def source_freeze(*, require_committed):
    changes = git('status', '--porcelain=v1', '--untracked-files=all', '--', *TRACKED_PATHS)
    if require_committed and changes:
        raise RuntimeError('REPORT_REPLAY_SOURCE_NOT_COMMITTED')
    files = [Path(__file__), ROOT / 'scripts/qa_narrative_v3.py',
             ROOT / 'scripts/qa_narrative_v3_closure.py', ROOT / 'pyproject.toml',
             ROOT / 'requirements.lock', ROOT / 'requirements-test.lock']
    return {'commit': git('rev-parse', 'HEAD'), 'source_changes': changes.splitlines(),
            'source_manifest_sha256': value_hash(source_manifest()),
            'prompt_bundle_sha256': value_hash(prompt_bundle_manifest()),
            'harness_files': [{'path': p.relative_to(ROOT).as_posix(),
                               'sha256': sha256(p.read_bytes()).hexdigest()} for p in files],
            'generation_policy': freeze_narrative_policy(POLICY_VERSION)}


def contracts():
    frozen = freeze_narrative_policy(POLICY_VERSION)
    return {'explanation': 'grounded_narrative_v3', 'writer_schema': 'explainer.v3',
            'reviewer_schema': 'narrative_review.v1',
            'report_generation_policy_version': POLICY_VERSION,
            'report_generation_policy': frozen,
            **{key: frozen[key] for key in ('evidence_builder_version', 'formatter_version', 'renderer_version')}}


def new_ledger(path, *, live):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.lock'):
        if path.exists():
            raise RuntimeError('REPORT_REPLAY_ALREADY_STARTED')
        ledger = {'schema_version': 'u1-report-replay-ledger/1.0', 'created_at': now(),
                  'mode': 'live' if live else 'offline_mock', 'limit_actual_dispatches': 4,
                  'role_limits': {role: 2 for role in ROLES}, 'attempts': [],
                  'external_dispatches': 0, 'fresh_objective_evaluations': 0,
                  'solver_calls': 0, 'connection_tests': 0, 'state': 'prepared'}
        dump(path, ledger)
    return ledger


def reserve_attempt(path, role, schema, invocation, context, operation):
    with FileLock(str(path) + '.lock'):
        ledger = read(path)
        if ledger['state'] not in {'prepared', 'running'}:
            raise RuntimeError('REPORT_REPLAY_CLOSED')
        if ROLES.get(role) != schema:
            raise RuntimeError('REPORT_REPLAY_ROLE_FORBIDDEN')
        rows = ledger['attempts']
        if any(row['invocation_id'] == invocation for row in rows):
            raise RuntimeError('REPORT_REPLAY_INVOCATION_REUSE_FORBIDDEN')
        used = {name: sum(row['role'] == name for row in rows) for name in ROLES}
        if len(rows) >= 4 or used[role] >= 2:
            raise RuntimeError('REPORT_REPLAY_BUDGET_EXHAUSTED')
        if role == 'explainer' and (len(rows) >= 3 or used['report_reviewer'] >= 2):
            raise RuntimeError('REPORT_REPLAY_REVIEW_CAPACITY_REQUIRED')
        rows.append({'invocation_id': invocation, 'role': role, 'schema_id': schema,
                     'operation': operation, 'request_hash': context['request_hash'],
                     'reserved_at': now(), 'dispatched_at': None, 'finished_at': None,
                     'status': 'reserved', 'call_id': None})
        ledger['state'] = 'running'
        dump(path, ledger)


def update_attempt(path, invocation, **updates):
    with FileLock(str(path) + '.lock'):
        ledger = read(path)
        row = next(row for row in ledger['attempts'] if row['invocation_id'] == invocation)
        row.update(updates)
        ledger['external_dispatches'] = sum(row['dispatched_at'] is not None for row in ledger['attempts'])
        dump(path, ledger)


class ReplayTransport(httpx.BaseTransport):
    """Charge each real HTTP entry once, before any bytes can reach the provider."""

    def __init__(self, repo, ledger_path, check_freeze):
        self.repo, self.ledger_path, self.check_freeze = repo, Path(ledger_path), check_freeze
        self.invocation = None
        self.inner = httpx.HTTPTransport(retries=0, verify=True, trust_env=False)

    def handle_request(self, request):
        self.check_freeze()
        if not self.invocation:
            raise RuntimeError('REPORT_REPLAY_UNBOUND_DISPATCH')
        with self.repo.connect() as db:
            row = db.execute('SELECT call_id,role,request_hash,input,metadata,profile_revision FROM llm_calls WHERE invocation_id=?',
                             (self.invocation,)).fetchone()
        if row is None:
            raise RuntimeError('REPORT_REPLAY_DISPATCH_WITHOUT_RESERVATION')
        proof = json.loads(row['metadata'] or '{}').get('provenance') or {}
        body_hash = sha256(request.content).hexdigest()
        policy = freeze_narrative_policy(POLICY_VERSION)
        if (proof.get('request_body_sha256') != body_hash
                or proof.get('report_generation_policy_version') != POLICY_VERSION
                or proof.get('report_generation_policy_hash') != policy['report_generation_policy_hash']):
            raise RuntimeError('REPORT_REPLAY_DISPATCH_BINDING_MISMATCH')
        body = json.loads(request.content)
        cap = body.get('max_completion_tokens', body.get('max_tokens'))
        if cap != policy['roles'][ROLES[row['role']]]['max_output_tokens']:
            raise RuntimeError('REPORT_REPLAY_OUTPUT_CAP_MISMATCH')
        with FileLock(str(self.ledger_path) + '.lock'):
            ledger = read(self.ledger_path)
            attempt = next((item for item in ledger['attempts'] if item['invocation_id'] == self.invocation), None)
            stored_input = json.loads(row['input'])
            if (attempt is None or attempt['role'] != row['role']
                    or row['request_hash'] != value_hash(stored_input)
                    or attempt['request_hash'] != stored_input.get('context', {}).get('request_hash')
                    or stored_input.get('invocation_id') != self.invocation
                    or stored_input.get('schema_id') != attempt['schema_id']):
                raise RuntimeError('REPORT_REPLAY_LEDGER_BINDING_MISMATCH')
            if attempt['dispatched_at'] is not None:
                raise RuntimeError('REPORT_REPLAY_HIDDEN_RETRY_FORBIDDEN')
            if ledger['external_dispatches'] >= 4 or ledger['state'] != 'running':
                raise RuntimeError('REPORT_REPLAY_BUDGET_EXHAUSTED')
            attempt.update(dispatched_at=now(), call_id=row['call_id'],
                           request_body_sha256=body_hash, profile_revision=row['profile_revision'],
                           effective_max_output_tokens=cap,
                           dispatch_semantics='entered real HTTP transport; provider receipt can remain unknown')
            ledger['external_dispatches'] += 1
            dump(self.ledger_path, ledger)
        return self.inner.handle_request(request)

    def close(self):
        # The gateway closes one Client per call; the runner owns the pool.
        pass

    def finish(self):
        self.inner.close()


def prepare_job(repo, report, snapshot):
    policy = deepcopy(report['policy'])
    policy.update(max_llm_calls=4, max_llm_calls_per_role=2, explain_via_llm=True)
    stamp = utc_now()
    with repo.transaction() as db:
        db.execute("INSERT INTO jobs(job_id,input,policy,llm_profile,job_status,phase,workflow,created_at,updated_at) "
                   "VALUES(?,?,?,?,'running','reporting',?,?,?)",
                   (report['job_id'], json_text(report['input']), json_text(policy), json_text(snapshot),
                    json_text({'execution_contracts': contracts(), 'runtime:creator': get_runtime()}), stamp, stamp))


def export_calls(repo, job_id, out, broker):
    with repo.connect() as db:
        rows = [dict(row) for row in db.execute(
            'SELECT call_id,invocation_id,role,attempt_no,status,input,output,previous_output,error,metadata,started_at,finished_at '
            'FROM llm_calls WHERE job_id=? ORDER BY started_at,rowid', (job_id,))]
    for row in rows:
        for key in ('input', 'output', 'error', 'metadata'):
            row[key] = json.loads(row[key]) if row[key] else None
    dump(out / 'invocations.json', rows, broker)
    journal = repo.get_job(job_id)['workflow'].get('narrative_v3_journal')
    dump(out / 'journal.json', journal, broker)
    return rows


def run_report_replay(source_report, evidence_dir, data_dir, *, source_profile_data=None, live=False):
    source_path, out, private = (Path(path).resolve() for path in (source_report, evidence_dir, data_dir))
    if private == out or private.is_relative_to(out) or out.is_relative_to(private):
        raise RuntimeError('REPORT_REPLAY_PRIVATE_DATA_MUST_BE_SEPARATE')
    if source_profile_data is not None and private == Path(source_profile_data).resolve():
        raise RuntimeError('REPORT_REPLAY_SOURCE_DATA_MUST_BE_PRESERVED')
    ledger_path = out / 'campaign_ledger.json'
    if ledger_path.exists():
        raise RuntimeError('REPORT_REPLAY_ALREADY_STARTED')
    if (private / 'state.sqlite3').exists():
        raise RuntimeError('REPORT_REPLAY_FRESH_DATA_REQUIRED')
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    if live and source.get('job_id') != SOURCE_JOB_ID:
        raise RuntimeError('REPORT_REPLAY_WRONG_SOURCE_JOB')
    if live and source_profile_data is None:
        raise RuntimeError('REPORT_REPLAY_SAVED_PROFILE_REQUIRED')
    before = core_hash(source)
    # Only the new report's generation contract changes. The stored computation
    # and historical report bytes remain immutable, including their old policy.
    reporting_source = deepcopy(source)
    reporting_source['execution_contracts'] = contracts()
    reporting_source['report_generation_policy_version'] = POLICY_VERSION
    if core_hash(reporting_source) != before:
        raise RuntimeError('REPORT_REPLAY_PROJECTION_CHANGED_CORE')
    frozen = source_freeze(require_committed=live)
    new_ledger(ledger_path, live=live)
    (out / 'source_report.json').write_bytes(source_bytes)
    manifest = {'schema_version': 'u1-report-replay/1.0', 'scope': 'report_only',
                'mode': 'live' if live else 'offline_mock', 'created_at': now(),
                'source_job_id': source['job_id'], 'source_report_sha256': sha256(source_bytes).hexdigest(),
                'source_core_sha256': before, 'max_actual_model_submissions': 4,
                'fresh_objective_evaluations_limit': 0, 'connection_tests_limit': 0,
                'freeze': frozen, 'private_data_dir': str(private),
                'historical_campaign_untouched': True}
    dump(out / 'run_manifest.json', manifest)

    def check_freeze():
        if source_freeze(require_committed=live) != frozen:
            raise RuntimeError('REPORT_REPLAY_FROZEN_SOURCE_CHANGED')
        if source_path.read_bytes() != source_bytes or (out / 'source_report.json').read_bytes() != source_bytes:
            raise RuntimeError('REPORT_REPLAY_SOURCE_REPORT_CHANGED')

    started = monotonic()
    transport = gateway = repo = None
    private.mkdir(parents=True, exist_ok=True)
    with instance_lock(private):
        try:
            repo = Repository(private)
            if live:
                copy_profile_tables(source_profile_data, repo)
                transport = ReplayTransport(repo, ledger_path, check_freeze)
            gateway = LLMGateway(repo, transport=transport if live else httpx.MockTransport(mock_provider))
            if not live:
                setup_offline_profile(gateway)
            status = gateway.get_profile()
            profile = status.get('profile')
            if (not profile or profile.get('test_status') != 'passed'
                    or not status.get('credential_status', {}).get('available')):
                raise RuntimeError('PROVIDER_CONFIGURATION_REQUIRED: saved valid profile and key required; no connection test sent')
            snapshot = gateway.profile_snapshot(profile['profile_id'], profile['revision'])
            manifest['profile'] = {key: profile[key] for key in ('profile_id', 'revision', 'model', 'protocol',
                                                               'response_mode', 'token_limit_field', 'max_output_tokens',
                                                               'request_timeout_seconds')}
            manifest['profile']['endpoint_sha256'] = sha256(profile['base_url'].encode()).hexdigest()
            dump(out / 'run_manifest.json', manifest)
            prepare_job(repo, source, snapshot)
            job_id = source['job_id']
            with repo.connect() as db:
                connection_rows_before = db.execute("SELECT COUNT(*) FROM llm_calls WHERE purpose='connection_test'").fetchone()[0]

            def persist(journal):
                repo.put_workflow(job_id, 'narrative_v3_journal', journal)

            def budget():
                attempts = read(ledger_path)['attempts']
                return {'total_used': len(attempts), 'max_total': 4, 'max_per_role': 2,
                        'by_role': {role: sum(row['role'] == role for row in attempts) for role in ROLES}}

            def invoke(role, schema, context, invocation):
                check_freeze()
                active = repo.get_job(job_id)['workflow']['narrative_v3_journal']['active_request']
                reserve_attempt(ledger_path, role, schema, invocation, context, active['operation'])
                identity = get_runtime()
                producers = repo.get_job(job_id)['workflow'].get('runtime:invocation_producers', {})
                producers[invocation] = {'process_run_id': identity['process_run_id'], 'role': identity['role'],
                                         'llm_role': role, 'schema_id': schema}
                repo.put_workflow(job_id, 'runtime:invocation_producers', producers)
                if transport:
                    transport.invocation = invocation
                try:
                    result = gateway.invoke(Invocation.model_validate({'job_id': job_id, 'role': role,
                                             'schema_id': schema, 'invocation_id': invocation, 'context': context}))
                except BaseException as error:
                    attempt = next(row for row in read(ledger_path)['attempts'] if row['invocation_id'] == invocation)
                    update_attempt(ledger_path, invocation, status='outcome_unknown' if attempt['dispatched_at'] else 'failed',
                                   finished_at=now(), error_code=getattr(error, 'error_code', type(error).__name__))
                    raise
                metadata = result.get('metadata') or {}
                update_attempt(ledger_path, invocation, status=result['status'], call_id=result.get('call_id'),
                               finished_at=now(), error_code=(result.get('error') or {}).get('error_code'),
                               usage=metadata.get('usage'), elapsed_seconds=metadata.get('elapsed_seconds'),
                               monetary_cost=None, monetary_cost_status='not_recorded')
                check_freeze()
                return result

            with no_numerics() as counters:
                payload = NarrativeOrchestrator(reporting_source, invoke=invoke, persist=persist, budget=budget,
                    current_report=lambda: deepcopy(reporting_source),
                    prompt_bundle_sha256=frozen['prompt_bundle_sha256'],
                    report_generation_policy_version=POLICY_VERSION).run()
                generated = apply_narrative_v3(reporting_source, payload)
                generated['execution_contracts'] = contracts()
                generated['report_generation_policy_version'] = POLICY_VERSION
                for key in ('llm_calls', 'timing', 'cost_estimate'):
                    generated['source_run_' + key] = deepcopy(source.get(key))
                generated['llm_calls'] = job_call_metadata(repo, job_id)
                generated['cost_estimate'] = None
                generated['timing'] = {**(source.get('timing') or {}), 'elapsed_wall_seconds': monotonic() - started,
                    'elapsed_llm_seconds': sum(row.get('elapsed_seconds') or 0 for row in generated['llm_calls']),
                    'elapsed_research_seconds': 0, 'elapsed_user_wait_seconds': 0}
                generated['qa_reporting'] = {'schema_version': 'u1-report-replay/1.0',
                    'scope': 'report_only', 'mode': manifest['mode'],
                    'source_job_id': job_id, 'source_fixture_sha256': manifest['source_report_sha256'],
                    'source_core_sha256': before, 'historical_numeric_compute_only': True,
                    'fresh_objective_evaluations': 0, 'solver_calls': 0, 'connection_tests': 0,
                    'description_fa': 'پذیرش گزارش بازتولیدشدهٔ U1 بر پایهٔ محاسبات ذخیره‌شده'}
                if core_hash(generated) != before:
                    raise RuntimeError('REPORT_REPLAY_NUMERICAL_CORE_CHANGED')
                dump(out / 'report.json', generated, gateway.broker)
                reopened = read(out / 'report.json')
                if core_hash(reopened) != before:
                    raise RuntimeError('REPORT_REPLAY_EXPORTED_CORE_CHANGED')
                html = html_bytes(reopened)
                (out / 'report.html').write_bytes(html)
                (out / 'reopened.html').write_bytes(html_bytes(read(out / 'report.json')))
                artifacts = [repo.save_artifact(job_id, 'report_json', (out / 'report.json').read_bytes(), '.json'),
                             repo.save_artifact(job_id, 'report_html', html, '.html')]
                repo.publish_report(job_id, reopened, [a['artifact_id'] for a in artifacts],
                    job_status='completed', solution_status=source['solution_status'],
                    explanation_status=generated['explanation_status'], optimality_status=source['optimality_status'])
            with repo.connect() as db:
                counts = {table: db.execute(f'SELECT COUNT(*) FROM {table} WHERE job_id=?', (job_id,)).fetchone()[0]
                          for table in ('trials', 'stage_executions')}
                connection_rows_after = db.execute("SELECT COUNT(*) FROM llm_calls WHERE purpose='connection_test'").fetchone()[0]
            if any(counts.values()) or any(counters.values()) or connection_rows_before != connection_rows_after:
                raise RuntimeError('REPORT_REPLAY_FORBIDDEN_WORK_DETECTED')
            calls = export_calls(repo, job_id, out, gateway.broker)
            accepted = sorted(row['section']['section_id'] for row in payload['accepted_sections'] if row['origin'] == 'llm')
            passed = set(REQUIRED_SECTIONS) <= set(accepted) and payload['mode'] == 'llm' and payload['review_status'] == 'passed'
            ledger = read(ledger_path)
            ledger.update(state='complete', result='PASS' if passed else 'FAIL', finished_at=now())
            dump(ledger_path, ledger)
            summary = {'schema_version': 'u1-report-replay-result/1.0', 'status': 'PASS' if passed else 'FAIL',
                'scope': 'report_only', 'mode': manifest['mode'], 'human_quality_and_ui_review': 'PENDING',
                'source_job_id': job_id, 'source_report_sha256': manifest['source_report_sha256'],
                'source_core_before': before, 'source_core_after': core_hash(reopened),
                'explanation_mode': payload['mode'], 'review_status': payload['review_status'],
                'accepted_model_sections': accepted, 'report_sha256': sha256((out / 'report.json').read_bytes()).hexdigest(),
                'role_attempts': len(ledger['attempts']), 'actual_model_submissions': ledger['external_dispatches'],
                'model_submissions_by_role': {role: sum(row['role'] == role and row['dispatched_at'] is not None
                                                       for row in ledger['attempts']) for role in ROLES},
                'connection_tests': 0, 'copied_historical_connection_proofs': connection_rows_before if live else 0,
                'offline_mock_connection_tests': connection_rows_before if not live else 0,
                'fresh_objective_evaluations': 0, 'solver_calls': 0, 'guards': counters, 'fresh_numerical_rows': counts,
                'usage': {key: sum((row.get('metadata', {}).get('usage') or {}).get(key) or 0 for row in calls)
                          for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
                'tested_commit': frozen['commit'], 'report_generation_policy_version': POLICY_VERSION}
            check_freeze()
            dump(out / 'summary.json', summary, gateway.broker)
            return summary
        except BaseException as error:
            if gateway and repo:
                with repo.connect() as db:
                    exists = db.execute('SELECT 1 FROM jobs WHERE job_id=?', (source['job_id'],)).fetchone()
                if exists:
                    export_calls(repo, source['job_id'], out, gateway.broker)
            ledger = read(ledger_path)
            ledger.update(state='failed', finished_at=now(), error_code=getattr(error, 'error_code', type(error).__name__))
            dump(ledger_path, ledger)
            dump(out / 'blocker.json', {'status': 'FAIL', 'error_type': type(error).__name__, 'reason': str(error)[:500],
                                      'actual_model_submissions': ledger['external_dispatches']}, gateway.broker if gateway else None)
            raise
        finally:
            if transport:
                transport.finish()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('source-report', 'evidence-dir', 'data-dir'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--source-profile-data', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    result = run_report_replay(args.source_report, args.evidence_dir, args.data_dir,
                              source_profile_data=args.source_profile_data, live=args.live)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
