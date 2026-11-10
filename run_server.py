"""Start or stop the local SolverScout instance from a Python editor or terminal."""

import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parent
    executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    python = root / ".venv" / executable
    if not python.is_file():
        setup = r"scripts\setup.bat" if os.name == "nt" else "./scripts/setup.sh"
        print(f"محیط Python 3.12 آماده نیست. یک‌بار {setup} را اجرا کنید.", file=sys.stderr)
        return 3

    args = sys.argv[1:]
    command = args.pop(0) if args[:1] == ["stop"] else "start"
    preview_config = root / ".solverscout-v2-preview" / "preview-config.json"
    overrides = {"--data-dir", "--config"}
    if preview_config.is_file() and not any(arg.partition("=")[0] in overrides for arg in args):
        args = ["--data-dir", str(preview_config.parent), "--config", str(preview_config), *args]

    try:
        return subprocess.call([str(python), "-m", "p_mc_optimizer", command, *args])
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
