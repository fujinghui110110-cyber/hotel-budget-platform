#!/usr/bin/env python3
"""Manage the official cloudflared quick tunnel and isolated HTTPS origin."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
STATE = RUNTIME / 'public-access.json'
LOGS = ROOT / 'logs'
PORT = 8769
URL_PATTERN = re.compile(r'https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com\b')


def public_environment(url):
    if not URL_PATTERN.fullmatch(url):
        raise ValueError('无效的 Cloudflare 公网地址')
    env = os.environ.copy()
    # Use the same database and application options as the local server.
    for name in ('.env.production', '.env'):
        path = ROOT / name
        if path.exists():
            for raw in path.read_text().splitlines():
                line = raw.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.removeprefix('export ').split('=', 1)
                    env.setdefault(key.strip(), value.strip().strip('\"\''))
    key_path = RUNTIME / 'public-secret-key'
    if not key_path.exists():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_urlsafe(64))
    env.update(DJANGO_SETTINGS_MODULE='config.settings', DJANGO_DEBUG='0',
               DJANGO_SECRET_KEY=key_path.read_text().strip(),
               DJANGO_ALLOWED_HOSTS=url.removeprefix('https://'),
               CSRF_TRUSTED_ORIGINS=url, TRUST_PROXY='1', SECURE_SSL_REDIRECT='1',
               SESSION_COOKIE_SECURE='1', CSRF_COOKIE_SECURE='1',
               BUDGET_PROCESS_UPLOAD_INLINE='0', PUBLIC_ACCESS='1')
    return env


def security_check(env):
    code = '''import django, sys
    django.setup()
    from django.contrib.auth import get_user_model
    candidates = ('admin123', 'project123', 'password', '123456', '12345678', 'admin', 'demo123', 'test123', '')
    weak = [u.username for u in get_user_model().objects.filter(is_active=True) if u.has_usable_password() and any(u.check_password(p) for p in candidates)]
    if weak:
        print('公网启动已阻止：以下账号仍使用已知示例或常见弱口令：' + '、'.join(weak))
        sys.exit(2)
    print('已知示例弱口令检查通过；请使用独立强密码。')
    '''
    import textwrap
    code = '\n'.join([code.splitlines()[0]] + [line[4:] for line in code.splitlines()[1:]])
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)], cwd=ROOT, env=env)
    if result.returncode:
        raise SystemExit(result.returncode)


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def supervisor_alive(pid):
    if not alive(pid):
        return False
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
    return str(Path(__file__).resolve()) in result.stdout and '_serve' in result.stdout


def status():
    if not STATE.exists():
        return {'running': False}
    data = json.loads(STATE.read_text())
    data['running'] = (data.get('status') in {'connecting', 'started'}
                       and supervisor_alive(data.get('pid'))
                       and all(alive(p) for p in data.get('children', [])))
    return data


def serve():
    lock = (RUNTIME / 'public-access.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit('公网服务已在运行')
    env = public_environment('https://startup-check.trycloudflare.com')
    security_check(env)
    binary = RUNTIME / 'bin' / 'cloudflared'
    if not binary.exists():
        raise SystemExit('缺少 .runtime/bin/cloudflared，请按 docs/公网接入.md 安装官方版本')
    children = []
    def stop(*_, error=None):
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        STATE.write_text(json.dumps({'pid': os.getpid(), 'children': [],
                                     'status': 'failed' if error else 'stopped',
                                     'error': error, 'running': False}))
        lock.close()
        raise SystemExit(1 if error else 0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Hold an idle-sleep assertion only while this supervisor is alive.
    # -w also releases the assertion if this process dies unexpectedly.
    if sys.platform == 'darwin':
        children.append(subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())]))
    # Explicit empty config prevents an unrelated named-tunnel configuration loading.
    config = RUNTIME / 'quick-tunnel.yml'
    config.write_text('{}\n')
    tunnel_log = LOGS / 'public-tunnel.log'
    with tunnel_log.open('w', buffering=1) as output:
        tunnel = subprocess.Popen([str(binary), 'tunnel', '--config', str(config),
            '--no-autoupdate', '--protocol', 'http2', '--url', f'http://127.0.0.1:{PORT}'],
            cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
        children.append(tunnel)
        STATE.write_text(json.dumps({'pid': os.getpid(), 'children': [child.pid for child in children], 'status': 'connecting'}))
        url = None
        for _ in range(120):
            match = URL_PATTERN.search(tunnel_log.read_text(errors='replace'))
            if match:
                url = match.group(0)
                break
            if any(child.poll() is not None for child in children):
                break
            time.sleep(1)
        if not url:
            print('未能取得公网地址，请检查 logs/public-tunnel.log', flush=True)
            stop(error='未能取得公网地址，检查 public-tunnel.log')
        env = public_environment(url)
        with (LOGS / 'public-web.log').open('a', buffering=1) as web_log:
            children.append(subprocess.Popen([sys.executable, '-m', 'gunicorn', 'config.wsgi:application',
                '--bind', f'127.0.0.1:{PORT}', '--workers', '1', '--threads', '4', '--timeout', '120',
                '--forwarded-allow-ips', '127.0.0.1', '--access-logfile', '-'],
                cwd=ROOT, env=env, stdout=web_log, stderr=subprocess.STDOUT))
            STATE.write_text(json.dumps({'pid': os.getpid(), 'children': [p.pid for p in children],
                                         'url': url, 'status': 'started'}))
            print(url, flush=True)
            while all(p.poll() is None for p in children):
                time.sleep(2)
            stop(error='公网子进程意外退出，检查 public-web.log 和 public-tunnel.log')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'status', 'check', '_serve'])
    args = parser.parse_args()
    RUNTIME.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    if args.action == 'check':
        security_check(public_environment('https://startup-check.trycloudflare.com'))
    elif args.action == 'status':
        print(json.dumps(status(), ensure_ascii=False))
    elif args.action == 'stop':
        data = status()
        if data.get('pid') and supervisor_alive(data['pid']):
            os.kill(data['pid'], signal.SIGTERM)
        print('已发送关闭请求；使用 status 确认停止。')
    elif args.action == '_serve':
        serve()
    elif status().get('running'):
        print(json.dumps(status(), ensure_ascii=False))
    else:
        security_check(public_environment('https://startup-check.trycloudflare.com'))
        subprocess.run([sys.executable, str(ROOT / 'scripts/local_server.py'), 'start'], cwd=ROOT, check=True)
        with (LOGS / 'public-supervisor.log').open('a') as output:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_serve'], cwd=ROOT,
                start_new_session=True, stdout=output, stderr=subprocess.STDOUT)
        for _ in range(55):
            data = status()
            if data.get('url') and data.get('running'):
                print(data['url'])
                return
            if process.poll() is not None:
                raise SystemExit('公网启动失败，查看 logs/public-supervisor.log')
            time.sleep(1)
        print('隧道仍在连接，稍后执行 status 查看地址。')


if __name__ == '__main__':
    main()
