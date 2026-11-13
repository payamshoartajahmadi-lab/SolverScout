"""Read-only Git/filesystem provenance plus a selected, secret-free evidence ZIP.

Run only after the recorded offline run finishes. Does not execute QA/app code.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import zipfile

EVIDENCE = Path(__file__).resolve().parent
ROOT = EVIDENCE.parents[2]
ORIGINAL = Path('G:/projects/solverscout')


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()


def canonical_hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_record(path, root=ROOT):
    data = path.read_bytes()
    return {'path': path.relative_to(root).as_posix(), 'bytes': len(data),
            'sha256': sha256(data).hexdigest()}


def save(name, value):
    (EVIDENCE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def runtime_manifest():
    paths = set()
    for folder in ('src/p_mc_optimizer', 'schemas'):
        paths.update(p for p in (ROOT / folder).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.suffix != '.pyc' and p.name != '.DS_Store')
    for name in ('run_server.py', 'pyproject.toml', 'requirements.lock', 'requirements-test.lock',
                 'scripts/start.sh', 'scripts/start.bat', 'scripts/setup.sh', 'scripts/setup.bat',
                 'examples/config.example.json'):
        if (ROOT / name).is_file():
            paths.add(ROOT / name)
    files = [file_record(p) for p in sorted(paths)]
    return {'files': files, 'canonical_source_sha256': canonical_hash(files)}


def harness_manifest():
    paths = {ROOT / 'scripts/qa_narrative_v3_closure.py', ROOT / 'scripts/qa_narrative_v3.py'}
    paths.update(p for p in (ROOT / 'tests').rglob('*') if p.is_file()
                 and p.suffix in {'.py', '.json', '.txt'} and '__pycache__' not in p.parts)
    files = [{k: v for k, v in file_record(p).items() if k != 'bytes'} for p in sorted(paths)]
    return {'files': files, 'canonical_harness_sha256': canonical_hash(files)}


def collect():
    sha = git('rev-parse', 'HEAD')
    full_runs = [json.loads(p.read_text(encoding='utf-8')) for p in (EVIDENCE / 'offline').glob('full-final-*-result.json')]
    full = max(full_runs, key=lambda run: run['started_at_utc'])
    if full['head_sha'] != sha or full['source_changes_at_start']:
        raise RuntimeError('Full run must refer to the current committed source')
    if any(full.get(key) is not True for key in ('source_bytes_unchanged_during_run', 'harness_bytes_unchanged_during_run')):
        raise RuntimeError('Source or harness bytes changed during the full run')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('Source changed after the recorded full run')
    source = runtime_manifest()
    if source['canonical_source_sha256'] != full['source_sha256']:
        raise RuntimeError('Current source bytes differ from the completed full run')
    source.update(tested_commit=sha, hash_definition='Matches existing closure runtime_manifest; working-file bytes, including line endings')
    save('source_manifest.json', source)
    harness = harness_manifest()
    if harness['canonical_harness_sha256'] != full['harness_sha256']:
        raise RuntimeError('Current harness bytes differ from the completed full run')
    harness.update(tested_commit=sha, hash_definition='Matches existing closure harness_manifest; working-file bytes')
    save('harness_manifest.json', harness)
    baseline = json.loads((EVIDENCE / 'baseline.json').read_text(encoding='utf-8'))
    ancestors = {commit: subprocess.run(['git', 'merge-base', '--is-ancestor', commit, sha], cwd=ROOT).returncode == 0
                 for commit in baseline['windows_commits'] + baseline['closure_commits']}
    docs = ['docs/FINAL_NARRATIVE_CLOSURE_2026_11_12_FA.md', 'docs/Delivery_Preparation_Handoff_FA.md']
    historical = {p: {'tested_blob': git('rev-parse', sha + ':' + p),
                      'closure_blob': git('rev-parse', baseline['fetch']['remote_closure'] + ':' + p)} for p in docs}
    branches_unchanged = {name: git('rev-parse', name) == baseline['local_' + name]
                          for name in ('dev', 'main')}
    if not all(ancestors.values()) or not all(branches_unchanged.values()):
        raise RuntimeError('Required ancestry or preservation of dev/main did not verify')
    if any(row['tested_blob'] != row['closure_blob'] for row in historical.values()):
        raise RuntimeError('A historical report changed')
    preserved = []
    for folder in ('artifacts/branch-transition/20261113T102853Z-main-to-dev',
                   'artifacts/status-recovery/20261113T102200Z-6837ba0'):
        preserved.extend(file_record(p, ORIGINAL) for p in sorted((ORIGINAL / folder).rglob('*')) if p.is_file())
    preservation = json.loads((ORIGINAL / 'artifacts/branch-transition/20261113T102853Z-main-to-dev/preservation.json').read_text(encoding='utf-8-sig'))
    backup_matches = {r['path']: sha256((ORIGINAL / 'artifacts/branch-transition/20261113T102853Z-main-to-dev' / r['path']).read_bytes()).hexdigest() == r['sha256']
                      for r in preservation['files']}
    recovery_zip = ORIGINAL / 'artifacts/status-recovery/20261113T102200Z-6837ba0/SolverScout_Recovery_Evidence.zip'
    recovery_unchanged = sha256(recovery_zip.read_bytes()).hexdigest() == '4b7daaa519bf7171356355340bf4a662d5694b6acdb3702af1ec5eeb0a41e264'
    if not all(backup_matches.values()) or not recovery_unchanged:
        raise RuntimeError('Historical backup or recovery ZIP preservation did not verify')
    save('preservation-check.json', {'files': preserved, 'original_test_backups_match_preservation_record': backup_matches,
         'recovery_zip_matches_recorded_hash': recovery_unchanged,
         'historical_documents': historical})
    save('git-provenance.json', {'recorded_at_utc': datetime.now(timezone.utc).isoformat(), 'tested_commit': sha,
         'branch': git('branch', '--show-current'), 'parents': git('show', '-s', '--format=%P', sha).split(),
         'dev': git('rev-parse', 'dev'), 'main': git('rev-parse', 'main'),
         'dev_main_unchanged': branches_unchanged,
         'current_bytes_match_full_run': {'source': True, 'harness': True},
         'origin_dev_after_fetch': baseline['fetch']['remote_dev'], 'closure_after_fetch': baseline['fetch']['remote_closure'],
         'required_commits_are_ancestors': ancestors,
         'changed_paths_since_integration': git('diff', '--name-only', baseline['integration_commit'], sha).splitlines(),
         'implementation_commit': '0a9908ea9f23081f6be559596765aa16911b8580',
         'source_git_tree': git('rev-parse', sha + ':src'), 'tests_git_tree': git('rev-parse', sha + ':tests'),
         'tracked_source_changes_after_tests': [], 'reports_will_be_committed_separately': True})
    patch = subprocess.check_output(['git', 'diff', '--no-ext-diff', baseline['integration_commit'], sha,
                                     '--', 'src', 'tests', '.gitattributes'], cwd=ROOT)
    (EVIDENCE / 'contract-repair.patch').write_bytes(patch)
    print(json.dumps({'tested_commit': sha, 'source_sha256': source['canonical_source_sha256'],
                      'harness_sha256': harness['canonical_harness_sha256'], 'ancestors': ancestors,
                      'backup_matches': backup_matches}, indent=2))


def bundle():
    # Explicitly reviewed mission files only; no recursive suffix-based export.
    names = {
        '.gitattributes', 'baseline.json', 'dependency-setup.json', 'source_manifest.json', 'harness_manifest.json',
        'remote-before-publication.json', 'schema-eol-diagnosis.json', 'numeric-startup-diagnosis.json',
        'offline/legacy-identifier-tie-diagnostic.json',
        'preservation-check.json', 'git-provenance.json', 'summary.json',
        'synthetic-repair-example.json', 'Contract_Repair_Status_FA.md', 'Next_Live_Acceptance_FA.md',
        'contract-repair.patch', 'run_offline.py', 'offline_network_guard.py',
        'collect_evidence.py', 'build_report.py', 'generate_synthetic_repair_example.py',
    }
    for label in ('targeted-new-01', 'targeted-final-01', 'targeted-final-02', 'full-final-01',
                  'compatibility-final-01', 'full-final-02'):
        if not (EVIDENCE / ('offline/' + label + '-result.json')).is_file():
            continue
        for ending in ('.log', '.xml', '-result.json', '-network-guard.json'):
            names.add('offline/' + label + ending)
        for ending in ('-source_manifest.json', '-harness_manifest.json', '-progress.jsonl'):
            if (EVIDENCE / ('offline/' + label + ending)).is_file():
                names.add('offline/' + label + ending)
    selected = [EVIDENCE / name for name in sorted(names)]
    for p in selected:
        if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(EVIDENCE.resolve()):
            raise RuntimeError('Missing or unsafe selected evidence path: ' + p.name)
    files = []
    run_records = {p.name.removesuffix('-result.json'): json.loads(p.read_text(encoding='utf-8'))
                   for p in (EVIDENCE / 'offline').glob('*-result.json')}
    tested_commit = json.loads((EVIDENCE / 'git-provenance.json').read_text(encoding='utf-8'))['tested_commit']
    for path in selected:
        row = file_record(path, EVIDENCE)
        row['relationship'] = ('actual_offline_execution' if 'offline' in path.parts else
                               'synthetic_not_historical' if 'synthetic' in path.name else 'provenance_or_report')
        row['version_relation'] = ('Per-run head SHA and dirty-state in the matching result JSON; per-run manifests when available'
                                   if 'offline' in path.parts else
                                   'Current tested source in git-provenance.json; synthetic samples carry their own source-file hashes')
        matching_label = next((label for label in run_records if path.name.startswith(label)), None)
        if matching_label:
            run = run_records[matching_label]
            row.update(run_label=matching_label, recorded_head_sha=run.get('head_sha'),
                       dirty_source_at_run_start=run.get('source_changes_at_start'))
            if 'head_sha' not in run or 'source_changes_at_start' not in run:
                row['version_relation'] = 'NOT_VERIFIED: this early run did not capture HEAD and dirty-source metadata; no version has been reconstructed'
        elif path.name in {'schema-eol-diagnosis.json', 'numeric-startup-diagnosis.json',
                           'legacy-identifier-tie-diagnostic.json'}:
            row.update(relationship='read_only_diagnosis_of_first_full_offline_run',
                       run_label='full-final-01', recorded_head_sha=run_records['full-final-01']['head_sha'],
                       version_relation='Original full-final-01 failure; not a fresh test or diagnosis of full-final-02')
        elif path.name == 'synthetic-repair-example.json':
            row['version_relation'] = 'Synthetic fixture; four exact source-file hashes and synthetic prompt hash are recorded within the sample; no historical C-L1 raw response'
        elif path.name in {'baseline.json', 'remote-before-publication.json', 'dependency-setup.json'}:
            row['version_relation'] = 'Observation at the time recorded in this file; not a test result for the final source'
        else:
            row['recorded_tested_commit'] = tested_commit
        files.append(row)
    save('evidence_index.json', {'scope': 'Only new mission evidence; no private database, env, credential store, cookie or connection headers',
         'numeric_execution.authorization_policy': 'Public numerical execution contract retained; no blanket key-name redaction',
         'index_and_archive_self_hash_excluded': True, 'files': files})
    archive = EVIDENCE / 'SolverScout_Contract_Repair_Evidence.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in selected + [EVIDENCE / 'evidence_index.json']:
            z.write(path, path.relative_to(EVIDENCE).as_posix())
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for row in files:
            assert sha256(z.read(row['path'])).hexdigest() == row['sha256']
    print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size,
                      'sha256': sha256(archive.read_bytes()).hexdigest(), 'selected_files': len(files)}))


if __name__ == '__main__':
    import sys
    {'collect': collect, 'bundle': bundle}[sys.argv[1]]()
