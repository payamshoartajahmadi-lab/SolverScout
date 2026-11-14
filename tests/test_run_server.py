"""Launcher routing regression: a preview file must not change the user store."""
from pathlib import Path
import os
import runpy
import sys

import pytest

from p_mc_optimizer import __main__ as cli


@pytest.mark.parametrize("command", ["start", "stop"])
@pytest.mark.parametrize("override", ["default", "data-dir", "config"])
def test_launch_store_is_independent_of_preview_and_working_directory(tmp_path, monkeypatch, command, override):
    script = Path(__file__).resolve().parents[1] / "run_server.py"
    main = runpy.run_path(str(script))["main"]
    project = tmp_path / "project"
    python = project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    preview = project / ".solverscout-v2-preview"
    preview.mkdir()
    (preview / "preview-config.json").write_text('{"api_port":60910,"ui_port":60911}', encoding="utf-8")
    main.__globals__["__file__"] = str(project / "run_server.py")
    user_store = tmp_path / "saved user data"
    monkeypatch.setattr("p_mc_optimizer.config.user_data_dir", lambda *args, **kwargs: str(user_store))
    unrelated = tmp_path / "unrelated directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    selected = []
    monkeypatch.setattr(cli, "start", lambda directory, config, **kwargs: selected.append((directory, config)))
    monkeypatch.setattr(cli, "stop", lambda directory: selected.append((directory, None)))
    forwarded = []

    def invoke(args):
        assert args[:3] == [str(python), "-m", "p_mc_optimizer"]
        forwarded.append(args[3:])
        return cli.main(args[3:])

    monkeypatch.setattr("subprocess.call", invoke)
    options = []
    expected_store = user_store
    if override == "data-dir":
        expected_store = tmp_path / "explicit data"
        options = ["--data-dir", str(expected_store)]
    elif override == "config":
        options = ["--config", str(preview / "preview-config.json")]
    argv = [str(script), *(["stop"] if command == "stop" else []), *options]
    monkeypatch.setattr(sys, "argv", argv.copy())

    assert main() == 0
    assert forwarded == [[command, *options]]
    assert sys.argv == argv
    assert selected[0][0] == expected_store.resolve()
    if command == "start":
        assert selected[0][1].ui_port == (60911 if override == "config" else 8501)
