#!/usr/bin/env python3
"""Manage the official cloudflared quick tunnel and isolated HTTPS origin."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time

try:
    from scripts.runtime_support import FileLock, pid_alive, process_matches, stop_process_tree, detached_popen_kwargs, wsgi_command, process_identity, stop_identities
except ImportError:
    from runtime_support import FileLock, pid_alive, process_matches, stop_process_tree, detached_popen_kwargs, wsgi_command, process_identity, stop_identities

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
STATE = RUNTIME / 'public-access.json'
OPERATION = RUNTIME / 'public-operation.json'
STOP_REQUEST = RUNTIME / 'public-stop-request'
LOGS = ROOT / 'logs'
PORT = 8769
URL_PATTERN = re.compile(r'https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com\b')


def public_environment(url):
    if not URL_PATTERN.fullmatch(url):
        raise ValueError('无效的 Cloudflare 公网地址')
    RUNTIME.mkdir(exist_ok=True)
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
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)], cwd=ROOT, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stdout.strip() or '公网启动安全检查失败，请检查服务配置及账号密码。')


def _write(path, data):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def alive(pid):
    return pid_alive(pid)


def supervisor_alive(pid):
    return process_matches(pid, Path(__file__).resolve(), '_serve')


def status():
    data = _read(STATE)
    data['running'] = bool(data.get('status') in {'connecting', 'started'}
                           and supervisor_alive(data.get('pid'))
                           and all(alive(p) for p in data.get('children', [])))
    if data.get('status') in {'connecting', 'started'} and not data['running']:
        data.update(status='failed', error='公网服务已退出，请重新生成链接。', url=None)
    data.setdefault('status', 'stopped')
    operation = _read(OPERATION)
    busy = bool(operation.get('status') in {'restarting', 'stopping'} and
                (process_matches(operation.get('pid'), Path(__file__).resolve(), '_control')
                 or (not operation.get('pid') and time.time() - operation.get('time', 0) < 10)))
    if busy:
        data.update(status=operation['status'], url=None, error=None)
    elif operation.get('status') == 'failed':
        data.update(status='failed', error=operation.get('error'), url=None)
    elif operation.get('status') in {'restarting', 'stopping'}:
        data.update(status='failed', error='公网控制进程已退出，请重试。', url=None)
    data['busy'] = busy or data['status'] == 'connecting'
    if not data['running']:
        data['url'] = None
    return data


def ensure_binary():
    binary = RUNTIME / 'bin' / ('cloudflared.exe' if sys.platform == 'win32' else 'cloudflared')
    if binary.exists():
        return binary
    import hashlib
    import platform
    import urllib.request
    machine = platform.machine().lower()
    if sys.platform == 'win32' and machine in {'amd64', 'x86_64', 'x86', 'i386', 'i686'}:
        asset_name = 'cloudflared-windows-' + ('amd64' if machine in {'amd64', 'x86_64'} else '386') + '.exe'
    elif sys.platform == 'darwin' and machine in {'arm64', 'aarch64', 'amd64', 'x86_64'}:
        asset_name = 'cloudflared-darwin-' + ('arm64' if machine in {'arm64', 'aarch64'} else 'amd64') + '.tgz'
    else:
        raise RuntimeError('当前平台没有配置官方 cloudflared 自动安装包，请按 docs/公网接入.md 安装。')
    request = urllib.request.Request('https://api.github.com/repos/cloudflare/cloudflared/releases/latest',
                                     headers={'User-Agent': 'hotel-budget-platform'})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next((item for item in release['assets'] if item['name'] == asset_name), None)
    if not asset or not re.fullmatch(r'sha256:[a-f0-9]{64}', asset.get('digest') or ''):
        raise RuntimeError('官方发布未提供可验证的 SHA256，已取消自动安装。')
    url = asset['browser_download_url']
    if not url.startswith('https://github.com/cloudflare/cloudflared/releases/download/'):
        raise RuntimeError('官方安装包地址校验失败。')
    binary.parent.mkdir(parents=True, exist_ok=True)
    temporary = binary.with_suffix('.download')
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != asset['digest'].split(':')[1]:
            raise RuntimeError('cloudflared 安装包 SHA256 校验失败。')
        if sys.platform == 'darwin':
            import shutil
            import tarfile
            from pathlib import PurePosixPath
            extracted = binary.with_suffix('.unpacked')
            try:
                with tarfile.open(temporary, 'r:gz') as archive:
                    members = archive.getmembers()
                    if len(members) != 1 or not members[0].isfile() or PurePosixPath(members[0].name) != PurePosixPath('cloudflared'):
                        raise RuntimeError('cloudflared 安装包内容不符合安全要求。')
                    with archive.extractfile(members[0]) as source, extracted.open('wb') as output:
                        shutil.copyfileobj(source, output)
                extracted.chmod(0o755)
                os.replace(extracted, binary)
            finally:
                extracted.unlink(missing_ok=True)
        else:
            os.replace(temporary, binary)
    finally:
        temporary.unlink(missing_ok=True)
    return binary


def _legacy_children(data):
    """Only adopt old state entries whose full command and working directory match."""
    commands = [
        [str(RUNTIME / 'bin' / ('cloudflared.exe' if sys.platform == 'win32' else 'cloudflared')),
         'tunnel', '--config', str(RUNTIME / 'quick-tunnel.yml'), '--no-autoupdate',
         '--protocol', 'http2', '--url', f'http://127.0.0.1:{PORT}'],
        wsgi_command(PORT, public=True),
        [sys.executable, '-m', 'gunicorn', 'config.wsgi:application', '--bind', f'127.0.0.1:{PORT}',
         '--workers', '1', '--threads', '4', '--timeout', '120', '--forwarded-allow-ips',
         '127.0.0.1', '--access-logfile', '-'],
        ['/usr/bin/caffeinate', '-i', '-w', str(data.get('pid'))],
    ]
    identities = []
    for pid in data.get('children', []):
        identity = process_identity(pid)
        if (identity and identity.get('cwd') == str(ROOT)
                and identity.get('cmdline') in commands):
            identities.append(identity)
    return identities


def _stop():
    data = _read(STATE)
    identities = data.get('children_identity')
    if identities is None:
        identities = _legacy_children(data)
    if supervisor_alive(data.get('pid')):
        STOP_REQUEST.write_text('stop', encoding='utf-8')
        # Existing POSIX supervisors also understand SIGTERM.
        if os.name != 'nt':
            try:
                os.kill(data['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 20
        while supervisor_alive(data['pid']) and time.monotonic() < deadline:
            time.sleep(.2)
        if supervisor_alive(data['pid']):
            stop_process_tree(data['pid'], timeout=10)
    stop_identities(identities, timeout=10)
    _write(STATE, {'status': 'stopped', 'running': False, 'children': [], 'children_identity': [], 'url': None})


def serve():
    with FileLock(RUNTIME / 'public-access.lock'):
        STOP_REQUEST.unlink(missing_ok=True)
        children = []
        error = None
        def interrupted(*_):
            raise InterruptedError('stop')
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            env = public_environment('https://startup-check.trycloudflare.com')
            security_check(env)
            binary = ensure_binary()
            if sys.platform == 'darwin':
                children.append(subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())]))
            config = RUNTIME / 'quick-tunnel.yml'
            config.write_text('{}\n', encoding='utf-8')
            tunnel_log = LOGS / 'public-tunnel.log'
            with tunnel_log.open('w', encoding='utf-8', buffering=1) as output:
                tunnel = subprocess.Popen([str(binary), 'tunnel', '--config', str(config),
                    '--no-autoupdate', '--protocol', 'http2', '--url', f'http://127.0.0.1:{PORT}'],
                    cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
            children.append(tunnel)
            _write(STATE, {'pid': os.getpid(), 'children': [p.pid for p in children],
                           'children_identity': [identity for p in children if (identity := process_identity(p.pid))], 'status': 'connecting'})
            url = None
            for _ in range(120):
                if STOP_REQUEST.exists():
                    raise InterruptedError('stop')
                match = URL_PATTERN.search(tunnel_log.read_text(encoding='utf-8', errors='replace'))
                if match:
                    url = match.group(0)
                    break
                if any(p.poll() is not None for p in children):
                    raise RuntimeError('公网隧道启动失败，请查看 logs/public-tunnel.log。')
                time.sleep(1)
            if not url:
                raise RuntimeError('公网链接生成超时，请检查网络后重试。')
            with (LOGS / 'public-web.log').open('a', encoding='utf-8', buffering=1) as output:
                children.append(subprocess.Popen(wsgi_command(PORT, public=True),
                    cwd=ROOT, env=public_environment(url), stdout=output, stderr=subprocess.STDOUT))
            # Do not advertise an origin that failed to bind/start.
            import socket
            for _ in range(60):
                if STOP_REQUEST.exists():
                    raise InterruptedError('stop')
                if any(p.poll() is not None for p in children):
                    raise RuntimeError('公网网页服务启动失败，请查看 logs/public-web.log。')
                try:
                    with socket.create_connection(('127.0.0.1', PORT), timeout=.5):
                        break
                except OSError:
                    time.sleep(.5)
            else:
                raise RuntimeError('公网网页服务启动超时。')
            _write(STATE, {'pid': os.getpid(), 'children': [p.pid for p in children],
                           'children_identity': [identity for p in children if (identity := process_identity(p.pid))],
                           'status': 'started', 'url': url})
            while all(p.poll() is None for p in children):
                if STOP_REQUEST.exists():
                    raise InterruptedError('stop')
                time.sleep(1)
            raise RuntimeError('公网子进程意外退出，请检查 public-web.log 和 public-tunnel.log。')
        except (InterruptedError, KeyboardInterrupt):
            pass
        except BaseException as exc:
            error = str(exc) or '公网服务启动失败。'
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            _write(STATE, {'pid': os.getpid(), 'children': [], 'status': 'failed' if error else 'stopped',
                           'error': error, 'url': None, 'running': False})
        if error:
            raise SystemExit(1)


def request_action(action):
    if action not in {'restart', 'stop'}:
        raise ValueError('不支持的公网操作')
    RUNTIME.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    try:
        with FileLock(RUNTIME / 'public-control.lock'):
            current = status()
            if current['busy']:
                return current
            token = secrets.token_hex(16)
            operation = {'status': 'restarting' if action == 'restart' else 'stopping',
                         'time': time.time(), 'token': token, 'pid': None}
            _write(OPERATION, operation)
            try:
                with (LOGS / 'public-controller.log').open('a', encoding='utf-8') as output:
                    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                        '_control', action, token], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                        **detached_popen_kwargs())
                operation['pid'] = process.pid
                _write(OPERATION, operation)
            except Exception as exc:
                _write(OPERATION, {**operation, 'status': 'failed', 'error': str(exc)})
            return status()
    except BlockingIOError:
        return status()


def control(action, token):
    # Parent holds the lock until it records this process' identity.
    for _ in range(100):
        try:
            with FileLock(RUNTIME / 'public-control.lock'):
                if _read(OPERATION).get('token') != token:
                    return
                break
        except BlockingIOError:
            time.sleep(.05)
    else:
        return
    try:
        _stop()
        if action == 'restart':
            # Validate before starting the new supervisor; failures reach the page.
            security_check(public_environment('https://startup-check.trycloudflare.com'))
            ensure_binary()
            with (LOGS / 'public-supervisor.log').open('a', encoding='utf-8') as output:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_serve'],
                    cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, **detached_popen_kwargs())
            for _ in range(180):
                state = _read(STATE)
                if state.get('pid') == process.pid:
                    if state.get('status') == 'started':
                        break
                    if state.get('status') == 'failed':
                        raise RuntimeError(state.get('error') or '公网启动失败。')
                if process.poll() is not None:
                    raise RuntimeError('公网服务启动失败，请检查 logs/public-supervisor.log。')
                time.sleep(1)
            else:
                _stop()
                raise RuntimeError('公网链接生成超时，请检查网络后重试。')
        _write(OPERATION, {'status': 'done', 'token': token})
    except BaseException as exc:
        _write(OPERATION, {'status': 'failed', 'token': token,
                          'error': str(exc) or '公网操作失败，请检查日志。'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'restart', 'stop', 'status', 'check', '_serve', '_control'])
    parser.add_argument('control_action', nargs='?', choices=['restart', 'stop'])
    parser.add_argument('token', nargs='?')
    args = parser.parse_args()
    RUNTIME.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    if args.action == 'check':
        security_check(public_environment('https://startup-check.trycloudflare.com'))
    elif args.action == 'status':
        print(json.dumps(status(), ensure_ascii=False))
    elif args.action == '_serve':
        serve()
    elif args.action == '_control':
        if not args.control_action or not args.token:
            parser.error('控制操作缺少参数')
        control(args.control_action, args.token)
    elif args.action == 'start' and status().get('running'):
        print(json.dumps(status(), ensure_ascii=False))
    else:
        if args.action in {'start', 'restart'}:
            subprocess.run([sys.executable, str(ROOT / 'scripts/local_server.py'), 'start'],
                           cwd=ROOT, check=True)
        print(json.dumps(request_action('stop' if args.action == 'stop' else 'restart'), ensure_ascii=False))


if __name__ == '__main__':
    main()
