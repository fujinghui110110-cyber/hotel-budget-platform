#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$ROOT_DIR"

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  . "$ROOT_DIR/.env"
  set +a
fi

export DJANGO_SETTINGS_MODULE=config.settings
VENV_DIR=${VENV_DIR:-"$ROOT_DIR/.venv"}
PYTHON="$VENV_DIR/bin/python"
PORT=${PORT:-8000}

case "$PORT" in
  ''|*[!0-9]*)
    echo "PORT 必须是数字：$PORT" >&2
    exit 1
    ;;
esac

if [ ! -x "$PYTHON" ]; then
  echo "未找到 $PYTHON。请先按 docs/SETUP_MAC.md 创建 .venv。" >&2
  exit 1
fi

exec "$PYTHON" manage.py runserver --noreload "127.0.0.1:$PORT" "$@"
