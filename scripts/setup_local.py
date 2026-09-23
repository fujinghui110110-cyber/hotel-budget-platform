"""Initialize a new computer without inserting demonstration business data."""
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_server import load_environment


def main():
    env_path = ROOT / '.env'
    if not env_path.exists():
        values = [f'DJANGO_SECRET_KEY={secrets.token_urlsafe(64)}', 'DJANGO_DEBUG=1', 'PORT=8768']
        # Preserve existing installations. Only brand-new instances select an external data directory.
        if not (ROOT / 'db.sqlite3').exists() and not any((ROOT / 'storage/uploads').glob('**/*.*')):
            default_data = (Path(os.getenv('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'HotelBudgetPlatform'
                            if sys.platform == 'win32' else Path.home() / 'Library/Application Support/HotelBudgetPlatform')
            data = Path(os.getenv('BUDGET_DATA_ROOT', default_data)).expanduser()
            data.mkdir(parents=True, exist_ok=True)
            if (data / 'db.sqlite3').exists():
                raise RuntimeError('目标业务目录已有数据库，请连接原安装配置，不能重新初始化。')
            values.extend(['DATABASE_PATH=' + str(data / 'db.sqlite3'),
                           'BUDGET_STORAGE_ROOT=' + str(data / 'storage')])
        if os.environ.get('SOFFICE_BIN'):
            values.append('SOFFICE_BIN=' + os.environ['SOFFICE_BIN'])
        with env_path.open('x', encoding='utf-8') as output:
            output.write('\n'.join(values) + '\n')
        env_path.chmod(0o600)
    load_environment()
    if os.environ.get('SOFFICE_BIN') and 'SOFFICE_BIN=' not in env_path.read_text(encoding='utf-8'):
        with env_path.open('a', encoding='utf-8') as output:
            output.write('\nSOFFICE_BIN=' + os.environ['SOFFICE_BIN'] + '\n')
    subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'migrate', '--noinput'], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'collectstatic', '--noinput'], cwd=ROOT, check=True)
    import django
    django.setup()
    from django.conf import settings
    settings.BUDGET_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    from scripts.admin_access import provision, reveal
    reveal(provision(ROOT))
    print('Local initialization complete. No sample projects or budgets were created.')


if __name__ == '__main__':
    main()
