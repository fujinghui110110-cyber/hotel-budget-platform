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
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    User = get_user_model()
    if not User.objects.filter(is_superuser=True).exists():
        print('Create the administrator account. Use a unique password of at least 12 characters.')
        subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'createsuperuser'], cwd=ROOT, check=True)
    print('Local initialization complete. No sample projects or budgets were created.')


if __name__ == '__main__':
    main()
