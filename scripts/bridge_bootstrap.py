"""Keep an older desktop shortcut pointing to the active verified release."""
import json
import os
from pathlib import Path
import sys


def dispatch_active_release(root):
    install = Path(os.getenv('BUDGET_INSTALL_ROOT', root)).resolve()
    pointer = install / 'active-release.json'
    if not pointer.exists():
        return
    state = json.loads(pointer.read_text(encoding='utf-8'))
    release = Path(state['release_dir']).resolve()
    python = Path(os.path.abspath(state['python']))
    launcher = release / 'scripts/local_server.py'
    if state.get('schema') != 1 or release.parent != install / 'releases':
        raise RuntimeError('当前版本记录无效，未启动其他程序。')
    if (release / '.venv').is_symlink() or not python.is_relative_to(release / '.venv') or not python.is_file() or not launcher.is_file():
        raise RuntimeError('当前版本运行环境不完整，请检查升级记录。')
    os.environ.setdefault('BUDGET_INSTALL_ROOT', str(install))
    os.environ.setdefault('BUDGET_RUNTIME_ROOT', str(install / '.runtime'))
    os.environ.setdefault('BUDGET_LOG_ROOT', str(install / 'logs'))
    os.environ.setdefault('DATABASE_PATH', str(install / 'db.sqlite3'))
    os.environ.setdefault('BUDGET_STORAGE_ROOT', str(install / 'storage'))
    os.environ.setdefault('BUDGET_TEMPLATE_ROOT', str(install))
    if Path(root).resolve() != release or os.path.abspath(sys.executable) != str(python):
        os.execv(str(python), [str(python), str(launcher), *sys.argv[1:]])
