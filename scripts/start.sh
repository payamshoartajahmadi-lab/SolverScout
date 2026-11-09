#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python="$root/.venv/bin/python"
if [ ! -x "$python" ]; then
    printf '%s\n' 'ابتدا scripts/setup.sh را اجرا کنید.' >&2
    exit 3
fi
exec "$python" -m p_mc_optimizer start "$@"
