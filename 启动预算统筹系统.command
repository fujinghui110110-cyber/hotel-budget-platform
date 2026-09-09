#!/bin/sh
set -eu
SYSTEM_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SYSTEM_ROOT"
PYTHON_BIN=${PYTHON_BIN:-"$SYSTEM_ROOT/.venv/bin/python"}
if [ ! -x "$PYTHON_BIN" ]; then
    echo "首次运行请先执行：sh scripts/setup_mac.sh"
    if [ -t 0 ]; then read -r REPLY; fi
    exit 1
fi
ACTION=${1:-start}
if [ "$#" -gt 0 ]; then shift; fi
if ! "$PYTHON_BIN" "$SYSTEM_ROOT/scripts/local_server.py" "$ACTION" --open "$@"; then
    echo "详细原因见 $SYSTEM_ROOT/logs/server.log"
    if [ -t 0 ]; then read -r REPLY; fi
    exit 1
fi
