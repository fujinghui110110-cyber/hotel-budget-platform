#!/bin/sh
set -eu
SYSTEM_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$SYSTEM_ROOT"
PYTHON_BIN=${PYTHON_BIN:-python3}
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 13), "请安装 Python 3.13 或更新版本"'
if [ ! -x .venv/bin/python ]; then
    "$PYTHON_BIN" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
if ! command -v soffice >/dev/null 2>&1 && [ ! -x /Applications/LibreOffice.app/Contents/MacOS/soffice ]; then
    echo "还需要安装 LibreOffice，或在 .env 中设置 SOFFICE_BIN。"
    exit 1
fi
echo "依赖已准备好。双击 启动预算统筹系统.command 即可启动。"
