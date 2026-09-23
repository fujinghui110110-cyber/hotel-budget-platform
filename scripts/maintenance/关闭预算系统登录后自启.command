#!/bin/sh
set -eu
SYSTEM_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
exec "$SYSTEM_ROOT/.venv/bin/python" "$SYSTEM_ROOT/scripts/local_server.py" disable-autostart
