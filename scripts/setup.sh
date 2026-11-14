#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
python=${PMC_PYTHON312:-python3.12}
if ! "$python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null; then
    printf '%s\n' 'Python 3.12 لازم است. مسیر آن را در PMC_PYTHON312 تعیین کنید.' >&2
    exit 3
fi
printf '%s\n' 'نصب وابستگی‌های قفل‌شده ممکن است به اینترنت نیاز داشته باشد.'
"$python" -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pip check
smoke_dir=$(mktemp -d "${TMPDIR:-/tmp}/pmc-smoke.XXXXXX")
trap 'rm -rf -- "$smoke_dir"' EXIT
.venv/bin/python -m p_mc_optimizer doctor --data-dir "$smoke_dir"
.venv/bin/python -m p_mc_optimizer solve --data-dir "$smoke_dir" --problem examples/quadratic_2d.json \
    --policy fixed_solver --solver de.original --budget 1002
