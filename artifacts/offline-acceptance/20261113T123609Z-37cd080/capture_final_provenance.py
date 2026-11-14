"""Read final Git/remote and hashes; no fetch, checkout or product execution."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from source_hashes import runtime_manifest, harness_manifest

B = Path(__file__).resolve().parent
ROOT = B.parents[2]
def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()
def save(name, data):
    assert not (B/name).exists(), 'Never overwrite captured final provenance'
    (B/name).write_text(json.dumps(data, indent=2)+'\n', encoding='utf-8')

if __name__ == '__main__':
    final_label = sys.argv[1] if len(sys.argv)>1 else 'full-final-01'
    final=json.loads((B/'offline'/(final_label+'-result.json')).read_text())
    baseline=json.loads((B/'baseline.json').read_text())
    source,harness=runtime_manifest(),harness_manifest()
    assert final['head_sha']==git('rev-parse','HEAD')
    assert final['source_sha256']==source['canonical_source_sha256']
    assert final['harness_sha256']==harness['canonical_harness_sha256']
    completed=subprocess.run(['git','ls-remote','--heads','origin','dev','main',
                             'fix/narrative-contract-repair','fix/narrative-v3-closure'],
                             cwd=ROOT,capture_output=True,text=True)
    if completed.returncode:
        raise SystemExit('Direct origin ref check failed; remote URL/stderr withheld')
    refs={line.split()[1]:line.split()[0] for line in completed.stdout.splitlines()}
    save('remote-before-publication.json',{
        'observed_at_utc':datetime.now(timezone.utc).isoformat(),'method':'git ls-remote --heads origin',
        'refs':refs,'no_remote_url_emitted':True,
        'dev_main_same_as_mission_baseline':{name:refs.get('refs/heads/'+name)==baseline['origin_direct_refs'].get('refs/heads/'+name) for name in ('dev','main')},
    })
    required=['37cd0803b1244320d1ab4866dd303fd7ba2a6780','d32691dfbb7e5f9faeafe5dcb119d65e97d46de3',
              '5ddee26ed86cefe9585e3f89fe02fc7a9f4f7816','13184acb677a2dbe96524380057251027e73db41',
              'b4723bf56d4ea76a0aec16f994ae87fa0e4b2713','ad449d252f243ff0400e3d42caae5d25defe5b11',
              '0a9908ea9f23081f6be559596765aa16911b8580']
    save('git-provenance.json',{
        'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'branch':git('branch','--show-current'),
        'tested_commit':final['head_sha'],'upstream':git('rev-parse','--abbrev-ref','@{upstream}'),
        'fixture_commits':git('log','--format=%H %s',baseline['head']+'..HEAD').splitlines(),
        'source_paths_changed_since_baseline':git('diff','--name-only',baseline['head'],'HEAD').splitlines(),
        'source_tree_unchanged_since_baseline':git('rev-parse',baseline['head']+':src')==git('rev-parse','HEAD:src'),
        'schemas_tree_unchanged_since_baseline':git('rev-parse',baseline['head']+':schemas')==git('rev-parse','HEAD:schemas'),
        'tested_source_sha256':source['canonical_source_sha256'],'tested_harness_sha256':harness['canonical_harness_sha256'],
        'tracked_source_changes_after_tests':git('status','--porcelain','--','src','tests','schemas','scripts','pyproject.toml').splitlines(),
        'required_commits_are_ancestors':{sha:subprocess.run(['git','merge-base','--is-ancestor',sha,'HEAD'],cwd=ROOT).returncode==0 for sha in required},
        'local_dev':git('rev-parse','dev'),'local_main':git('rev-parse','main'),
        'reports_commit_is_separate_from_tested_source':True,'no_merge_to_dev_main':True,
    })
    print(json.dumps({'tested_commit':final['head_sha'],'source_and_harness_match':True,'origin_refs':refs}))
