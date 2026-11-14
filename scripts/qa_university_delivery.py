#!/usr/bin/env python3
"""Explicit bounded U1/U2 acceptance through the installed application pipeline.

This is a thin adapter over the existing campaign ledger, gateway and worker.
It never manufactures model output, resets a job, or starts a third problem.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, UTC
from hashlib import sha256
import html
import json
import math
from pathlib import Path
import secrets
import socket
import sys
from threading import Thread, local, Lock
from time import monotonic, sleep
from uuid import uuid4

import httpx
from filelock import FileLock

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / 'src', ROOT / 'scripts'):
    if str(entry) not in sys.path: sys.path.insert(0, str(entry))

from qa_narrative_v3 import reserve_campaign_call, record_campaign_result, copy_profile_tables
from qa_narrative_v3_closure import dump
from p_mc_optimizer.api import create_app
from p_mc_optimizer.application.demo import TARGETS
from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.launcher import instance_lock
from p_mc_optimizer.llm.narrative_models import REQUIRED_SECTIONS
from p_mc_optimizer.llm.provider import prompt_bundle_manifest
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.reporting import html_bytes, csv_bytes, TRIAL_COLUMNS
from p_mc_optimizer.reporting.evidence_v3 import value_hash, core_hash
from p_mc_optimizer.research.contracts_v21 import CreateJobV21, SyncLimits
from p_mc_optimizer.research.library_retrieval import has_target_evidence
from p_mc_optimizer.runtime_provenance import source_manifest
from p_mc_optimizer.workflow.worker import run_claimed

SETTINGS = {'master_seed': 1405, 'max_objective_evaluations': 20000, 'max_compute_seconds': 300,
            'pilot_repeats': 3, 'pilot_evaluations': 500, 'max_llm_calls': 8,
            'max_llm_calls_per_role': 2, 'use_history': False, 'numeric_atol': 1e-12, 'numeric_rtol': 1e-10}
CASES = {'U1': {'file': 'university_u1_fa.txt', 'threshold': 1e-6, 'retrieval_mode': 'direct_refresh'},
         'U2': {'file': 'university_u2_fa.txt', 'threshold': 1e-3, 'retrieval_mode': 'local_only'}}
ENV_ERRORS = {'PROVIDER_CONFIGURATION_REQUIRED', 'PROVIDER_AUTH_FAILED', 'PROVIDER_UNREACHABLE',
              'PROVIDER_TIMEOUT', 'PROVIDER_RATE_LIMITED', 'PROVIDER_SERVER_ERROR',
              'SECURE_KEYRING_UNAVAILABLE', 'LLM_OUTCOME_UNKNOWN', 'LLM_INVOCATION_PENDING'}


def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def now(): return datetime.now(UTC).isoformat()
def file_hash(path): return sha256(Path(path).read_bytes()).hexdigest()


def validate_manifest(evidence_dir):
    manifest = read(Path(evidence_dir) / 'acceptance_manifest.json')
    if (manifest.get('limit_role_calls') != 20 or manifest.get('scenario_limits') != {'U1': 8, 'U2': 8, 'U-REPORT': 4}
            or manifest.get('objective_evaluations_limit') != 40000 or manifest.get('settings') != SETTINGS):
        raise RuntimeError('ACCEPTANCE_MANIFEST_LIMITS_MISMATCH')
    if not manifest.get('campaign_id'): raise RuntimeError('ACCEPTANCE_CAMPAIGN_ID_MISSING')
    if manifest.get('source_manifest_sha256') != value_hash(source_manifest()): raise RuntimeError('ACCEPTANCE_SOURCE_CHANGED')
    if manifest.get('prompt_bundle_sha256') != value_hash(prompt_bundle_manifest()): raise RuntimeError('ACCEPTANCE_PROMPTS_CHANGED')
    for key, case in CASES.items():
        expected = {**{k: case[k] for k in ('threshold', 'retrieval_mode')}, 'input_sha256': file_hash(ROOT / 'examples' / case['file'])}
        if manifest.get('scenarios', {}).get(key) != expected: raise RuntimeError('ACCEPTANCE_INPUT_OR_CRITERIA_CHANGED:' + key)
    return manifest


def ledger_path(data_dir): return Path(data_dir) / 'university-acceptance' / 'campaign_ledger.json'


def prepare_ledger(data_dir, manifest):
    path = ledger_path(data_dir); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(path) + '.lock'):
        if path.exists():
            state = read(path)
            if (state.get('campaign_id') != manifest['campaign_id'] or state.get('limit_role_calls') != 20
                    or state.get('manifest_sha256') != value_hash(manifest)):
                raise RuntimeError('ACCEPTANCE_LEDGER_BINDING_MISMATCH')
        else:
            dump(path, {'schema_version': 'university-acceptance/1.0', 'campaign_id': manifest['campaign_id'],
                        'manifest_sha256': value_hash(manifest), 'limit_role_calls': 20,
                        'attempts': [], 'role_calls': 0, 'stop_reason': None, 'created_at_utc': now()})
        path.chmod(0o600)
    return path


def update_ledger(path, *, invocation=None, changes=None, stop_reason=None):
    with FileLock(str(path) + '.lock'):
        state = read(path)
        if invocation is not None:
            row = next(r for r in state['attempts'] if r['invocation_id'] == invocation)
            row.update(changes or {})
        if stop_reason is not None and not state.get('stop_reason'): state['stop_reason'] = stop_reason
        dump(path, state)


class CampaignTransport(httpx.BaseTransport):
    """Charge once immediately before entering the real zero-retry transport."""
    def __init__(self, repo, ledger, scenario):
        self.repo, self.ledger, self.scenario = repo, Path(ledger), scenario
        self.active, self.lock, self.failure = local(), Lock(), None
        self.inner = httpx.HTTPTransport(retries=0, verify=True, trust_env=False)

    def fatal(self, reason):
        self.failure = reason; update_ledger(self.ledger, stop_reason=reason)
        raise RuntimeError(reason)

    def handle_request(self, request):
        invocation = getattr(self.active, 'invocation', None)
        if invocation is None: self.fatal('UNBOUND_EXTERNAL_DISPATCH')
        with self.lock:
            state = read(self.ledger)
            if state.get('stop_reason'): raise RuntimeError(state['stop_reason'])
            with self.repo.connect() as db:
                row = db.execute('SELECT call_id,status,request_hash,role,metadata,profile_revision FROM llm_calls WHERE invocation_id=? AND job_id=?',
                                 (str(invocation.invocation_id), str(invocation.job_id))).fetchone()
            if row is None or row['status'] != 'reserved': self.fatal('DISPATCH_WITHOUT_DURABLE_GATEWAY_RESERVATION')
            metadata = json.loads(row['metadata'] or '{}'); proof = metadata.get('provenance') or {}
            digest = sha256(request.content).hexdigest()
            if proof.get('request_body_sha256') != digest: self.fatal('DISPATCH_BODY_MISMATCH')
            prior = [r for r in state['attempts'] if r['scenario_id'] == self.scenario]
            if sum(r['role'] == invocation.role for r in prior) >= 2: self.fatal('CAMPAIGN_PER_ROLE_BUDGET_EXHAUSTED')
            if invocation.role in {'explainer', 'report_reviewer'} and sum(r['role'] in {'explainer', 'report_reviewer'} for r in prior) >= 4:
                self.fatal('CAMPAIGN_REPORT_BUDGET_EXHAUSTED')
            body = json.loads(request.content)
            cap = body.get('max_completion_tokens', body.get('max_tokens'))
            if cap != proof.get('effective_max_output_tokens'): self.fatal('DISPATCH_OUTPUT_CAP_MISMATCH')
            try:
                fresh = reserve_campaign_call(self.ledger, self.scenario, str(invocation.invocation_id),
                                              row['request_hash'], invocation.role, invocation.schema_id)
            except RuntimeError as error:
                self.fatal(str(error))
            if not fresh: self.fatal('HIDDEN_EXTERNAL_RETRY')
            update_ledger(self.ledger, invocation=str(invocation.invocation_id), changes={
                'call_id': row['call_id'], 'request_body_sha256': digest,
                'profile_revision': row['profile_revision'], 'dispatched_at_utc': now(),
                'effective_max_output_tokens': body.get('max_completion_tokens', body.get('max_tokens')),
                'dispatch_semantics': 'entered real HTTPTransport; provider receipt may remain unknown'})
        return self.inner.handle_request(request)

    def bind(self, gateway):
        original = gateway.invoke
        def invoke(invocation):
            self.active.invocation = invocation
            try:
                result = original(invocation)
                state = read(self.ledger)
                if any(r['invocation_id'] == str(invocation.invocation_id) for r in state['attempts']):
                    with self.repo.connect() as db:
                        row = db.execute('SELECT metadata FROM llm_calls WHERE invocation_id=?', (str(invocation.invocation_id),)).fetchone()
                    record_campaign_result(self.ledger, str(invocation.invocation_id), result, json.loads(row['metadata'] or '{}') if row else {})
                code = (result.get('error') or {}).get('error_code')
                if result.get('status') in {'unknown', 'outcome_unknown', 'reserved'} or code in ENV_ERRORS:
                    update_ledger(self.ledger, stop_reason=code or 'PROVIDER_RESULT_UNKNOWN')
                if self.failure: raise RuntimeError(self.failure)
                return result
            finally:
                self.active.invocation = None
        gateway.invoke = invoke

    def close(self): pass  # Each provider Client closes its transport; pool lifetime belongs to this runner.
    def finish(self): self.inner.close()


@contextmanager
def local_server(app):
    import uvicorn
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen(128)
    address = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=sock.getsockname()[1],
                                          log_level='error', access_log=False, lifespan='on'))
    thread = Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True); thread.start()
    try:
        deadline = monotonic() + 20
        while not server.started:
            if not thread.is_alive() or monotonic() >= deadline: raise RuntimeError('LOCAL_API_START_FAILED')
            sleep(0.05)
        yield address
    finally:
        server.should_exit = True; thread.join(timeout=10); sock.close()


def request_for(app, scenario, text):
    profile = app.state.gateway.get_profile()
    if not profile.get('profile') or not profile.get('credential_status', {}).get('available'):
        raise RuntimeError('PROVIDER_CONFIGURATION_REQUIRED')
    if profile['profile'].get('test_status') != 'passed': raise RuntimeError('PROVIDER_CONFIGURATION_REQUIRED')
    if not app.state.preparation.status()['ready']: raise RuntimeError('DEMO_PREPARATION_NOT_READY')
    catalog = app.state.catalog.snapshot()
    if not set(TARGETS) <= {c.implementation_id for c in catalog.cards}: raise RuntimeError('SIX_QUALIFIED_METHODS_REQUIRED')
    sources = app.state.search_settings.get_sources()
    config = next(row['config'] for row in app.state.library.list_sources()['sources'] if row['rule']['source_id'] == 'mealpy_docs')
    policy = {'policy_version': '2.1', 'portfolio_id': 'continuous_catalog_v2', 'selection_policy': 'evidence_research_then_pilot',
              'max_objective_evaluations': 20000, 'max_compute_seconds': 300, 'master_seed': 1405, 'explain_via_llm': True,
              'retrieval_mode': CASES[scenario]['retrieval_mode'], 'max_evidence_age_hours': 168,
              'stale_evidence_policy': 'reject', 'on_research_failure': 'stop',
              'sync_limits': SyncLimits().model_dump(mode='json') if scenario == 'U1' else None}
    return CreateJobV21.model_validate({'input': {'kind': 'natural_language', 'text': text}, 'policy': policy,
        'llm_profile': {'profile_id': profile['profile']['profile_id'], 'revision': profile['profile']['revision']},
        'catalog_hash': catalog.catalog_hash, 'source_revision': sources.revision,
        'source_config_refs': [{'source_id': 'mealpy_docs', 'revision': config['revision']}] if scenario == 'U1' else []})


def bind_job(repo, service, request, state_dir, manifest, scenario):
    path = Path(state_dir) / (scenario + '.binding.json')
    digest = value_hash(request.model_dump(mode='json'))
    if path.exists():
        binding = read(path)
        if binding['campaign_id'] != manifest['campaign_id'] or binding['request_hash'] != digest:
            raise RuntimeError('SCENARIO_JOB_BINDING_CHANGED')
        return repo.get_job(binding['job_id'])
    key = 'university:' + value_hash({'campaign': manifest['campaign_id'], 'scenario': scenario})
    job = service.create_job(request, key)
    dump(path, {'campaign_id': manifest['campaign_id'], 'scenario': scenario, 'job_id': job['job_id'], 'request_hash': digest})
    path.chmod(0o600)
    return repo.get_job(job['job_id'])


def invocation_context(row):
    # Gateway persistence stores Invocation.model_dump(), not just its context.
    value = row.get('input') or {}
    return value.get('context', value) if isinstance(value, dict) else {}


def independent_checks(report, invocations, scenario):
    advisor = [invocation_context(r) for r in invocations if r['role'] == 'advisor' and r.get('input')]
    cards = advisor[-1].get('solver_cards', []) if advisor else []
    sent = {c['implementation_id'] for c in cards}
    nominees = (report.get('advisor_plan_v2') or report.get('candidate_plan') or {}).get('candidates', [])
    ids = [c.get('implementation_id', c.get('algorithm_id')) for c in nominees]
    pilots = [r for r in report.get('trials', []) if r.get('phase') == 'pilot']
    grouped = {identity: [r for r in pilots if r['algorithm_id'] == identity] for identity in ids}
    valid_pilots = len(pilots) == 9 and len(grouped) == 3 and all(len(rows) == 3 and
        {r['replicate_index'] for r in rows} == {0, 1, 2} and all(r.get('execution_status') == 'completed' and
        r.get('evaluation_quota') == 500 and r.get('started_evaluations') == 500 and
        type(r.get('score_for_minimization')) in (float, int) and math.isfinite(r['score_for_minimization']) for r in rows)
        for rows in grouped.values())
    independent = {}
    if valid_pilots:
        scale = max([1.0, *(abs(r['score_for_minimization']) for rows in grouped.values() for r in rows)])
        for identity, rows in grouped.items():
            scores = sorted(r['score_for_minimization'] for r in rows)
            import numpy as np
            low, high = np.quantile(np.array(scores, dtype=np.float64) / scale, [0.25, 0.75], method='linear')
            independent[identity] = {'median_score': scores[1], 'normalized_iqr': float(high - low)}
    decision = report.get('decision') or {}
    metrics = {r['algorithm_id']: r for r in decision.get('metrics', [])}
    ranked = sorted(independent, key=lambda identity: (independent[identity]['median_score'], independent[identity]['normalized_iqr'], identity))
    winner = ranked[0] if ranked else None
    metrics_match = bool(independent) and set(metrics) == set(independent) and all(
        metrics[k].get('eligible') is True and metrics[k]['median_score'] == v['median_score'] and
        math.isclose(metrics[k]['normalized_iqr'], v['normalized_iqr'], rel_tol=1e-12, abs_tol=1e-15)
        for k, v in independent.items())
    x = report.get('best_x'); variables = ((report.get('problem_revision') or {}).get('spec') or {}).get('variables', [])
    finite = isinstance(x, list) and len(x) == 2 and all(type(v) in (int, float) and math.isfinite(v) for v in x)
    bounds = finite and len(variables) == 2 and all(v['lower'] <= point <= v['upper'] for point, v in zip(x, variables))
    value = report.get('objective_raw'); verified = report.get('verification') or {}
    narrative = report.get('narrative_v3') or {}
    accepted = {s['section']['section_id'] for s in narrative.get('accepted_sections', []) if s.get('origin') == 'llm' and s.get('reviewer_invocation_id')}
    context = advisor[-1] if advisor else {}
    passages = context.get('evidence', [])
    covered = [identity for identity in TARGETS if any(has_target_evidence(p['text'], identity) for p in passages)]
    coverage = (report.get('research') or {}).get('query_plan_target_coverage') or {}
    checks = {'six_target_cards_really_sent': set(TARGETS) <= sent and len(cards) >= 6,
              'three_distinct_eligible_nominees': len(ids) == len(set(ids)) == 3 and set(ids) <= sent,
              'six_targets_in_research_plan': set(TARGETS) <= set(coverage.get('targeted_implementation_ids', [])),
              'six_specific_evidence_passages': len(covered) == 6 and len(passages) <= 12 and sum(len(p['text']) for p in passages) <= 18000,
              'nine_equal_budget_pilots': valid_pilots,
              'independent_pilot_metrics_match': metrics_match,
              'winner_matches_empirical_order': bool(winner) and winner == decision.get('selected_algorithm_id') == report.get('selected_algorithm_id'),
              'finite_point_within_bounds': bool(bounds),
              'objective_within_fixed_threshold': type(value) in (int, float) and math.isfinite(value) and abs(value) <= CASES[scenario]['threshold'],
              'independent_function_verification_passed': verified.get('validation_status') == 'passed' and verified.get('numeric_atol') == 1e-12 and verified.get('numeric_rtol') == 1e-10,
              'objective_budget_respected': type((report.get('budget') or {}).get('known_started')) is int and report['budget']['known_started'] <= 20000,
              'five_required_llm_sections': narrative.get('mode') == 'llm' and set(REQUIRED_SECTIONS) <= accepted,
              'job_completed': report.get('job_status') == 'completed' and report.get('solution_status') == 'valid_best_found'}
    return {'checks': checks, 'advisor_sent_count': len(cards), 'advisor_sent_implementation_ids': sorted(sent),
            'candidate_implementation_ids': ids, 'independent_pilot_metrics': independent,
            'independent_winner': winner, 'specific_evidence_coverage': covered}


def export_run(repo, job, scenario, evidence_dir, broker, ledger, library=None):
    out = Path(evidence_dir) / 'live' / scenario; out.mkdir(parents=True, exist_ok=True)
    with repo.connect() as db:
        rows = [dict(row) for row in db.execute('SELECT call_id,invocation_id,role,status,input,output,previous_output,error,metadata FROM llm_calls WHERE job_id=? ORDER BY rowid', (job['job_id'],))]
    for row in rows:
        for field in ('input', 'output', 'error', 'metadata'): row[field] = json.loads(row[field]) if row[field] else None
    dump(out / 'invocations.json', rows, broker)
    dump(out / 'workflow.json', job['workflow'], broker)
    dump(out / 'job.json', job, broker)
    try: report = repo.get_report(job['job_id'])['report']
    except ApiError: report = None
    if report is not None:
        dump(out / 'report.json', report, broker)
        reopened = read(out / 'report.json')
        rendered = html_bytes(reopened).decode('utf-8')
        banner = '<aside data-delivery-saved-report="true" style="padding:1rem;text-align:right;direction:rtl">گزارش ذخیره‌شده · ' + html.escape(job['job_id']) + '</aside>'
        rendered = rendered.replace('<body>', '<body>' + banner, 1)
        if 'data-delivery-saved-report' not in rendered: raise RuntimeError('SAVED_REPORT_BANNER_MISSING')
        (out / 'report.html').write_text(rendered, encoding='utf-8')
        (out / 'trials.csv').write_bytes(csv_bytes(report.get('trials', []), TRIAL_COLUMNS))
        audit = independent_checks(report, rows, scenario)
        if library is not None:
            documents = {d['document_id']: d for d in job['workflow'].get('research_outcome', {}).get('documents', [])}
            advisor = [invocation_context(r) for r in rows if r['role'] == 'advisor' and r.get('input')]
            evidence = advisor[-1].get('evidence', []) if advisor else []
            integrity = bool(evidence)
            for document in documents.values():
                digest = document.get('text_sha256')
                text = library.load_text(digest) if digest else None
                if text is not None:
                    folder = out / 'documents'; folder.mkdir(exist_ok=True)
                    (folder / (digest + '.txt')).write_text(text, encoding='utf-8')
                for passage in (p for p in evidence if p['document_id'] == document['document_id']):
                    integrity = integrity and text is not None and sha256(text.encode('utf-8')).hexdigest() == digest and passage['text'] == text[passage['start']:passage['end']]
            integrity = integrity and all(p['document_id'] in documents for p in evidence)
            audit['checks']['evidence_offsets_and_document_hashes_match'] = bool(integrity)
            dump(out / 'documents.json', list(documents.values()), broker)
        status = 'PASS' if all(audit['checks'].values()) else 'FAIL'
        summary = {'scenario': scenario, 'status': status, 'job_id': job['job_id'], **audit,
                   'best_x': report.get('best_x'), 'objective_raw': report.get('objective_raw'),
                   'selected_algorithm_id': report.get('selected_algorithm_id'), 'solution_origin': report.get('solution_origin'),
                   'objective_evaluations': (report.get('budget') or {}).get('known_started'),
                   'narrative_mode': (report.get('narrative_v3') or {}).get('mode'),
                   'source_core_sha256': core_hash(report)}
    else:
        summary = {'scenario': scenario, 'status': 'BLOCKED', 'job_id': job['job_id'],
                   'reason': job.get('blocked_reason') or job.get('error') or job['job_status'],
                   'objective_evaluations': (job.get('budget') or {}).get('known_started', 0)}
    state = read(ledger); attempts = [r for r in state['attempts'] if r['scenario_id'] == scenario]
    if state.get('stop_reason'):
        summary['status'] = 'BLOCKED' if state['stop_reason'] in ENV_ERRORS or 'BUDGET_EXHAUSTED' in state['stop_reason'] else 'FAIL'
        summary['stop_reason'] = state['stop_reason']
    summary.update(real_role_calls=sum(bool(r.get('dispatched_at_utc')) for r in attempts),
                   charged_role_attempts=len(attempts), campaign_role_calls=len(state['attempts']),
                   cost_status='reported' if any(r.get('cost') is not None for r in attempts) else 'not_recorded',
                   role_usage=[{k: r.get(k) for k in ('role', 'call_id', 'status', 'usage', 'cost', 'elapsed_seconds')} for r in attempts])
    dump(out / 'summary.json', summary, broker); dump(Path(evidence_dir) / 'acceptance_campaign_ledger.json', state, broker)
    return summary


def run(data_dir, evidence_dir, scenario, source_profile_data, *, live=False):
    if not live: raise RuntimeError('EXPLICIT_LIVE_FLAG_REQUIRED')
    manifest = validate_manifest(evidence_dir)
    directory = Path(data_dir).resolve()
    with instance_lock(directory):
        repo = Repository(directory)
        ledger = prepare_ledger(directory, manifest)
        with repo.connect() as db: configured = bool(db.execute('SELECT 1 FROM active_provider_profile').fetchone())
        if not configured: copy_profile_tables(source_profile_data, repo)
        transport = CampaignTransport(repo, ledger, scenario)
        token = secrets.token_urlsafe(32)
        app = create_app(directory, token, transport=transport); transport.bind(app.state.gateway)
        try:
            request = request_for(app, scenario, (ROOT / 'examples' / CASES[scenario]['file']).read_text(encoding='utf-8'))
            job = bind_job(repo, app.state.service, request, ledger.parent, manifest, scenario)
            if job['job_status'] == 'queued' and not read(ledger).get('stop_reason'):
                with local_server(app) as address:
                    worker = str(uuid4()); claimed = repo.claim_job(worker, job_id=job['job_id'])
                    if claimed is not None: run_claimed(repo, claimed, worker, api_url=address, token=token)
            return export_run(repo, repo.get_job(job['job_id']), scenario, evidence_dir, app.state.gateway.broker, ledger, app.state.library)
        finally: transport.finish()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path); parser.add_argument('--evidence-dir', required=True, type=Path)
    parser.add_argument('--scenario', required=True, choices=tuple(CASES)); parser.add_argument('--source-profile-data', required=True, type=Path)
    parser.add_argument('--live', action='store_true'); args = parser.parse_args(argv)
    try:
        summary = run(args.data_dir, args.evidence_dir, args.scenario, args.source_profile_data, live=args.live)
    except Exception as error:
        reason = error.error_code if isinstance(error, ApiError) else str(error) if isinstance(error, RuntimeError) else type(error).__name__
        summary = {'scenario': args.scenario, 'status': 'BLOCKED', 'reason': reason[:240], 'exception_type': type(error).__name__}
        dump(args.evidence_dir / 'live' / args.scenario / 'blocked.json', summary)
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0 if summary['status'] == 'PASS' else 1


if __name__ == '__main__': raise SystemExit(main())
