"""Read filesystem bytes; no application or QA execution."""
from hashlib import sha256
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]

def canonical_hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def file_record(path, root=ROOT):
    data = path.read_bytes()
    return {'path': path.relative_to(root).as_posix(), 'bytes': len(data),
            'sha256': sha256(data).hexdigest()}

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
