#!/bin/sh
set -eu
SYSTEM_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SYSTEM_ROOT"
PYTHON_BIN=${PYTHON_BIN:-"$SYSTEM_ROOT/.venv/bin/python"}
if [ ! -x "$PYTHON_BIN" ]; then
    echo "首次使用，正在准备预算系统……"
    if ! sh "$SYSTEM_ROOT/scripts/setup_mac.sh"; then
        echo "准备未完成，请查看上面的提示。"
        if [ -t 0 ]; then read -r REPLY; fi
        exit 1
    fi
fi
echo "正在启动预算系统，准备好后会自动打开网页……"
if ! "$PYTHON_BIN" "$SYSTEM_ROOT/scripts/local_server.py" start --open "$@"; then
    echo "启动未完成，详细原因见 $SYSTEM_ROOT/logs/server.log"
    if [ -t 0 ]; then read -r REPLY; fi
    exit 1
fi
