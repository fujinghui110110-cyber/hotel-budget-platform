#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
exec "$ROOT/.venv/bin/python" scripts/public_access.py start
