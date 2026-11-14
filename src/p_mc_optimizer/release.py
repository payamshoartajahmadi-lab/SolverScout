"""Build the public source archive from an explicit set of source trees and file types."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from p_mc_optimizer import __version__

ROOT_FILES = {
    "pyproject.toml", "requirements.lock", "requirements-test.lock", "README.md", "README_FA.md",
    "IMPLEMENTATION_STATUS_V2.md", "TEST_RESULTS_V2.md", "TEST_RESULTS_V21.md",
    "QUICKSTART_FA.md", "IMPLEMENTATION_STATUS.md", "TEST_RESULTS.md", "RELEASE_NOTES.md", "run_server.py",
    "DELIVERY_GUIDE_FA.md",
}
TREE_FILES = {
    "src": {".py", ".txt", ".md", ".json", ".woff2"},
    "scripts": {".sh", ".bat", ".py"},
    "examples": {".json", ".txt"},
    "prompts": {".txt", ".md"},
    "schemas": {".json"},
    "tests": {".py", ".json", ".txt"},
    "ui": {".py", ".toml"},
    "docs": {".md"},
}
EXPLICIT_FILES = {"prompts/SHA256SUMS"}


def build_zip(root: Path, output: Path) -> Path:
    root, output = root.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"p_mc_optimizer-{__version__}.zip"
    files = sorted(ROOT_FILES | EXPLICIT_FILES | {
        path.relative_to(root).as_posix()
        for directory, suffixes in TREE_FILES.items()
        for path in (root / directory).rglob("*")
        if path.is_file() and path.suffix in suffixes
        and not any(part.startswith(".") or part.endswith(".egg-info") or part == "__pycache__"
                    for part in path.relative_to(root).parts)
        and path.resolve().is_relative_to(root)
    })
    try:
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"],
                                         stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    manifest = {"project": "p_mc_optimizer", "version": __version__, "commit": commit,
                "built_at": datetime.now(timezone.utc).isoformat(), "files": []}
    temp = archive.with_suffix(".zip.part")
    with ZipFile(temp, "w", ZIP_DEFLATED) as bundle:
        for name in files:
            path = root / name
            data = path.read_bytes()
            info = ZipInfo.from_file(path.resolve(), arcname=name)
            info.compress_type = ZIP_DEFLATED
            bundle.writestr(info, data)
            manifest["files"].append({"path": name, "size": len(data),
                                      "sha256": hashlib.sha256(data).hexdigest()})
        bundle.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    temp.replace(archive)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    return archive
