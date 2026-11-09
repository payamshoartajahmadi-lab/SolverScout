#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$root/.venv/bin/python" -m p_mc_optimizer package "$@"
