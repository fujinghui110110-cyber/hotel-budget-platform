#!/bin/sh
set -eu

SCRIPT_DIR=${0%/*}
if [ "$SCRIPT_DIR" = "$0" ]; then
    SCRIPT_DIR=.
fi
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR" && pwd)
cd "$ROOT_DIR"

if [ -f "$ROOT_DIR/.env" ]; then
    set -a
    . "$ROOT_DIR/.env"
    set +a
fi
export DJANGO_SETTINGS_MODULE=config.settings

PYTHON="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "未找到 .venv/bin/python。请先运行 ./启动平台.command 或按 docs/SETUP_MAC.md 准备环境。" >&2
    exit 1
fi

exec "$PYTHON" manage.py budget_worker "$@"
