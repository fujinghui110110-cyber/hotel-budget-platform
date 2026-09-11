#!/bin/sh
set -eu
SYSTEM_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SYSTEM_ROOT"
exec "$SYSTEM_ROOT/.venv/bin/python" scripts/stop_all.py
