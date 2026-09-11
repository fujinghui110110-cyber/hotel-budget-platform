"""Local administrator updates from the private repository's stable GitHub release."""
from contextlib import closing
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

try:
    from scripts.runtime_support import FileLock, detached_popen_kwargs, process_identity, identity_matches, process_matches, stop_process_tree, stop_identities
except ImportError:
    from runtime_support import FileLock, detached_popen_kwargs, process_identity, identity_matches, process_matches, stop_process_tree, stop_identities

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'fujinghui110110-cyber/hotel-budget-platform'
API = 'https://api.github.com/repos/' + REPOSITORY
ASSET_NAME = 'budget-system-update.zip'
CODE_DIRS = {'budgeting', 'config', 'scripts', 'templates', 'static', 'docs'}
CODE_FILES = {'manage.py', 'requirements.txt', 'README.md', 'system_version.json', 'requirements-update.lock'}
MAX_ARCHIVE = 100 * 1024 * 1024


class UpdateError(ValueError, RuntimeError):
    """A message safe to display without credentials or remote response bodies."""


def runtime(name):
    return ROOT / '.runtime' / name


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {} if default is None else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}\.\d+', value):
        raise UpdateError('发布版本号格式不正确。')
    return tuple(map(int, value.split('.')))


def current_version():
    return read_json(ROOT / 'system_version.json').get('version', '2026.09.11.0')


def status():
    state = read_json(runtime('update-state.json'))
    busy = bool(state.get('busy'))
    stale_launch = busy and not state.get('process') and time.time() - state.get('updated_at', runtime('update-state.json').stat().st_mtime) > 30
    if busy and ((state.get('process') and not identity_matches(state['process'])) or stale_launch):
        recovery_needed = runtime('update-journal.json').exists() or runtime('update-maintenance').exists()
        state.update(busy=False, status='interrupted', error=('更新进程已中断。请在服务器运行恢复命令，详情见系统更新说明。' if recovery_needed else '更新启动已中断，程序和数据尚未替换。请重新检查更新。'))
    release = read_json(runtime('update-release.json'))
    return {**state, 'current_version': current_version(),
            'available_version': release.get('version', ''),
            'release_notes': release.get('notes', ''),
            'configured': bool(read_json(runtime('update-config.json')).get('token')),
            'update_available': bool(release and version_key(release['version']) > version_key(current_version())),
            'busy': state.get('busy', False), 'error': state.get('error', ''),
            'status': state.get('status', 'idle')}


def configure(token):
    token = token.strip()
    if not 20 <= len(token) <= 300 or not re.fullmatch(r'[A-Za-z0-9_]+', token):
        raise UpdateError('请输入有效的 GitHub 只读访问令牌。')
    with FileLock(runtime('update.lock')):
        if status()['busy']:
            raise UpdateError('系统正在更新，请稍后配置。')
        target = runtime('update-config.json')
        write_json(target, {'token': token})
        if sys.platform == 'win32':
            # chmod alone does not restrict Windows ACLs. Use the current SID, not a localized name.
            sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True).strip().split(',')[-1].strip('"')
            result = subprocess.run(['icacls', str(target), '/inheritance:r', '/grant:r', '*' + sid + ':F', '*S-1-5-18:F'], capture_output=True)
            if result.returncode:
                target.unlink(missing_ok=True)
                raise UpdateError('无法保护下载凭据的文件权限，配置未保存。')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_open(url, accept='application/vnd.github+json'):
    if not url.startswith(API + '/'):
        raise UpdateError('拒绝访问非指定仓库的下载地址。')
    token = read_json(runtime('update-config.json')).get('token')
    if not token:
        raise UpdateError('请先配置此仓库的 GitHub 只读下载凭据。')
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token,
        'User-Agent': 'hotel-budget-updater', 'Accept': accept, 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        return urllib.request.build_opener(NoRedirect).open(request, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308) and accept == 'application/octet-stream':
            location = exc.headers.get('Location', '')
            parsed = urllib.parse.urlsplit(location)
            if parsed.scheme != 'https' or parsed.hostname not in {'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}:
                raise UpdateError('发布文件下载地址校验失败。') from None
            # Never forward private repository credentials to the asset host.
            return urllib.request.build_opener(NoRedirect).open(urllib.request.Request(location, headers={'User-Agent': 'hotel-budget-updater'}), timeout=60)
        raise UpdateError({401: 'GitHub 下载凭据无效或已过期。', 403: 'GitHub 拒绝访问，请检查令牌权限或稍后重试。',
                            404: '未找到稳定发布版本，或令牌没有此私有仓库的读取权限。'}.get(exc.code, 'GitHub 服务暂时不可用，请稍后重试。')) from None
    except (OSError, urllib.error.URLError):
        raise UpdateError('连接 GitHub 失败，请检查服务器网络后重试。') from None


def check():
    with FileLock(runtime('update.lock')):
        if status()['busy']:
            raise UpdateError('正在更新，请稍后检查。')
        with github_open(API + '/releases/latest') as response:
            release = json.loads(response.read(2 * 1024 * 1024))
        version = release.get('tag_name', '').removeprefix('v')
        version_key(version)
        asset = next((item for item in release.get('assets', []) if item.get('name') == ASSET_NAME), None)
        if release.get('draft') or release.get('prerelease') or not asset or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest') or ''):
            raise UpdateError('稳定版本缺少可校验的更新包，请联系维护人员。')
        if not isinstance(asset.get('id'), int) or not 0 < asset.get('size', 0) <= MAX_ARCHIVE:
            raise UpdateError('发布文件大小或编号不正确。')
        write_json(runtime('update-release.json'), {'version': version, 'notes': (release.get('body') or '')[:20000],
                   'asset_id': asset['id'], 'sha256': asset['digest'][7:], 'size': asset['size']})
        write_json(runtime('update-state.json'), {'status': 'checked', 'busy': False, 'error': '', 'message': '版本检查完成。'})
        return status()


def allowed_path(name):
    path = PurePosixPath(name)
    return (bool(name) and '\\' not in name and ':' not in name and not path.is_absolute()
            and all(part not in ('', '.', '..') and not part.startswith('.') and part != '__pycache__' for part in name.split('/'))
            and path.suffix.lower() not in {'.sqlite', '.sqlite3', '.db', '.xlsx', '.xlsm', '.exe', '.msi', '.pyc', '.pyo'}
            and not any(part.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(n) for n in range(1,10)), *('LPT'+str(n) for n in range(1,10))} or part.endswith((' ', '.')) for part in path.parts)
            and (path.parts[0] in CODE_DIRS or name in CODE_FILES or (len(path.parts) == 1 and path.suffix in {'.bat', '.command'})))


def validate_archive(archive, target, expected_version):
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = [item.filename for item in entries]
        if len(names) != len(set(name.casefold() for name in names)) or len(names) > 10000 or sum(i.file_size for i in entries) > MAX_ARCHIVE * 4:
            raise UpdateError('更新包存在重复文件或大小异常。')
        if 'manifest.json' not in names:
            raise UpdateError('更新包缺少文件校验清单。')
        manifest = json.loads(package.read('manifest.json'))
        files = manifest.get('files', {})
        if manifest.get('schema') != 1 or manifest.get('version') != expected_version or not isinstance(files, dict):
            raise UpdateError('更新包版本或格式不匹配。')
        if set(names) != {'manifest.json', *files} or not {'manage.py', 'requirements.txt', 'system_version.json', 'scripts/local_server.py', 'scripts/system_update.py'} <= files.keys():
            raise UpdateError('更新包文件清单不完整。')
        for item in entries:
            if item.filename == 'manifest.json':
                continue
            if not allowed_path(item.filename) or stat.S_ISLNK(item.external_attr >> 16):
                raise UpdateError('更新包含有不允许覆盖的路径。')
            data = package.read(item)
            if hashlib.sha256(data).hexdigest() != files[item.filename]:
                raise UpdateError('更新包文件校验失败。')
            path = target / item.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            if path.suffix == '.command':
                path.chmod(0o755)
        if read_json(target / 'system_version.json').get('version') != expected_version:
            raise UpdateError('程序版本与更新清单不一致。')
        return manifest


def state(phase, **extra):
    old = read_json(runtime('update-state.json'))
    write_json(runtime('update-state.json'), {**old, 'status': phase, 'updated_at': time.time(), **extra})


def start_update():
    with FileLock(runtime('update.lock')):
        info = status()
        if info['busy']:
            return info
        if runtime('update-journal.json').exists() or runtime('update-maintenance').exists():
            raise UpdateError('上次更新未完成，请先执行恢复命令。')
        if not info['update_available']:
            raise UpdateError('请先检查更新，当前没有可安装的新版本。')
        (ROOT / 'logs').mkdir(exist_ok=True)
        state('starting', busy=True, error='', message='正在准备更新。', process=None)
        try:
            with (ROOT / 'logs/system-update.log').open('a', encoding='utf-8') as log:
                child = subprocess.Popen([sys.executable, str(ROOT / 'scripts/system_update.py'), 'launch'], cwd=ROOT,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **detached_popen_kwargs())
            child.wait(timeout=15)
            if child.returncode:
                raise OSError('launcher failed')
        except OSError:
            state('failed', busy=False, error='无法启动更新进程，请检查日志目录权限。')
            raise UpdateError('无法启动更新进程。') from None
        return status()


def run(command, *, cwd=None, timeout=300):
    # Output stays local; credentials never appear in a command or child environment.
    result = subprocess.run([str(part) for part in command], cwd=cwd or ROOT, timeout=timeout,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise UpdateError('更新检查或服务命令执行失败：' + Path(str(command[0])).name + '，请检查运行环境。')
    return result.stdout


def python_in(folder):
    return folder / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')


def managed_files():
    candidates = list(ROOT.iterdir())
    for directory in CODE_DIRS:
        path = ROOT / directory
        if path.is_symlink():
            raise UpdateError('程序目录存在符号链接，已停止更新。')
        if path.exists():
            candidates.extend(path.rglob('*'))
    return [p.relative_to(ROOT).as_posix() for p in candidates
            if p.is_file() and allowed_path(p.relative_to(ROOT).as_posix())]


def settings_paths():
    # Same config precedence as the server, without importing application models.
    env = os.environ.copy()
    for name in ('.env.production', '.env'):
        for raw in (ROOT / name).read_text().splitlines() if (ROOT / name).exists() else []:
            line = raw.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.removeprefix('export ').split('=', 1)
                env.setdefault(key.strip(), value.strip().strip('\"\''))
    database = Path(env.get('DATABASE_PATH', str(ROOT / 'db.sqlite3')))
    if not database.is_absolute():
        database = ROOT / database
    storage = Path(env.get('BUDGET_STORAGE_ROOT', str(ROOT / 'storage')))
    if not storage.is_absolute():
        storage = ROOT / storage
    for path in (database, storage):
        if any(path.resolve().is_relative_to((ROOT / directory).resolve()) for directory in CODE_DIRS):
            raise UpdateError('数据目录位于程序目录内，需先调整数据保存位置再更新。')
    return database


def backup_code(destination):
    files = managed_files()
    for name in files:
        source = ROOT / name
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents if parent.is_relative_to(ROOT)):
            raise UpdateError('程序目录存在符号链接，已停止更新。')
        target = destination / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for name in ('.env', '.env.production'):
        if (ROOT / name).exists():
            target = destination / name
            shutil.copy2(ROOT / name, target)
            target.chmod(0o600)
    return files


def replace_code(source, names, remove):
    for name in remove:
        if not allowed_path(name):
            raise UpdateError('恢复清单路径无效。')
        (ROOT / name).unlink(missing_ok=True)
    for name in names:
        if not allowed_path(name):
            raise UpdateError('更新清单路径无效。')
        target = ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)


def start_server(python, port):
    run([python, ROOT / 'scripts/local_server.py', 'start', '--port', str(port)], timeout=180)


def restore(journal):
    backup = Path(journal['backup'])
    running = read_json(runtime('server.json'))
    if process_matches(running.get('pid'), ROOT / 'scripts/local_server.py', 'serve'):
        stop_process_tree(running['pid'])
    stop_identities(running.get('children_identity', []))
    runtime('server.json').unlink(missing_ok=True)
    replace_code(backup / 'code', journal['old_files'], set(journal['new_files']) - set(journal['old_files']))
    database = Path(journal['database'])
    if journal['database_existed']:
        for suffix in ('-wal', '-shm'):
            Path(str(database) + suffix).unlink(missing_ok=True)
        shutil.copy2(backup / 'database.sqlite3', database)
    else:
        database.unlink(missing_ok=True)
    pointer = runtime('active-python.json')
    if journal['old_python_pointer']:
        write_json(pointer, journal['old_python_pointer'])
    else:
        pointer.unlink(missing_ok=True)
    write_json(runtime('installed-files.json'), journal.get('old_installed_files', []))
    start_server(journal['old_python'], journal['port'])
    runtime('update-journal.json').unlink(missing_ok=True)
    runtime('update-maintenance').unlink(missing_ok=True)


def apply_update():
    with FileLock(runtime('update-worker.lock')):
        # Wait until start_update has recorded the detached worker's identity.
        time.sleep(.2)
        release = read_json(runtime('update-release.json'))
        journal = None
        stopped = False
        old_python = sys.executable
        server = read_json(runtime('server.json'))
        port = server.get('port', 8768)
        public_was_running = read_json(runtime('public-access.json')).get('running', False)
        operation = runtime('updates') / (time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
        operation.mkdir(parents=True, mode=0o700)
        try:
            state('downloading', busy=True, message='正在下载并校验新版本。')
            archive = operation / 'update.zip'
            digest = hashlib.sha256()
            count = 0
            with github_open(API + '/releases/assets/' + str(release['asset_id']), 'application/octet-stream') as response, archive.open('wb') as output:
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_ARCHIVE:
                        raise UpdateError('更新文件过大，已停止下载。')
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != release['sha256'] or count != release['size']:
                raise UpdateError('下载文件校验失败，请重新检查更新。')
            candidate = operation / 'candidate'
            manifest = validate_archive(archive, candidate, release['version'])
            new_python = old_python
            if (candidate / 'requirements.txt').read_bytes() != (ROOT / 'requirements.txt').read_bytes():
                state('preparing', message='正在独立环境中安装新版本依赖，原环境保持可用。')
                lock = candidate / 'requirements-update.lock'
                if not lock.exists():
                    raise UpdateError('新版本改变了依赖，但缺少带哈希的依赖锁定文件，请联系发布者。')
                environment = operation / 'environment'
                run([old_python, '-m', 'venv', environment])
                new_python = str(python_in(environment))
                run([new_python, '-m', 'pip', 'install', '--only-binary=:all:', '--require-hashes', '-r', lock, '-r', candidate / 'requirements.txt'], timeout=900)
                run([new_python, '-m', 'pip', 'check'])
            run([new_python, '-m', 'compileall', '-q', candidate / 'budgeting', candidate / 'config', candidate / 'scripts'])
            database = settings_paths()
            backup = operation / 'backup'
            backup.mkdir(mode=0o700)
            old_files = backup_code(backup)
            state('backing_up', message='正在暂停服务并备份数据库，请稍候。')
            runtime('update-maintenance').write_text('updating', encoding='utf-8')
            stopped = True
            run([old_python, ROOT / 'scripts/stop_all.py'], timeout=330)
            existed = database.exists()
            if existed:
                with closing(sqlite3.connect(str(database))) as source, closing(sqlite3.connect(str(backup / 'database.sqlite3'))) as target:
                    source.backup(target)
                (backup / 'database.sqlite3').chmod(0o600)
            journal = {'backup': str(backup), 'old_files': old_files, 'new_files': list(manifest['files']),
                       'database': str(database), 'database_existed': existed, 'port': port,
                       'old_python': old_python, 'old_python_pointer': read_json(runtime('active-python.json')),
                       'old_installed_files': read_json(runtime('installed-files.json'), [])}
            write_json(runtime('update-journal.json'), journal)
            state('installing', message='正在安装新版本并升级数据库。')
            replace_code(candidate, manifest['files'], set(journal['old_installed_files']) - set(manifest['files']))
            write_json(runtime('active-python.json'), {'path': new_python})
            run([new_python, ROOT / 'manage.py', 'check'])
            run([new_python, ROOT / 'manage.py', 'migrate', '--noinput'])
            start_server(new_python, port)
            write_json(runtime('installed-files.json'), list(manifest['files']))
            runtime('update-journal.json').unlink(missing_ok=True)
            runtime('update-maintenance').unlink(missing_ok=True)
            state('completed', busy=False, error='', message='更新成功。' + ('公网访问已暂停，请在公网访问页面重新生成链接。' if public_was_running else ''), backup=str(backup))
        except Exception as exc:
            # Exception text is constrained to our messages; URLs and credentials never reach status/logs.
            message = str(exc) if isinstance(exc, UpdateError) else '更新失败，请检查网络、磁盘空间和运行环境。'
            if journal:
                try:
                    state('rolling_back', message='更新未成功，正在恢复旧版本。')
                    restore(journal)
                    state('rolled_back', busy=False, error=message, message='已恢复旧版本，原有预算数据保持完整。')
                except Exception:
                    state('recovery_required', busy=False, error='自动恢复未完成，请按系统更新说明执行恢复命令。', message='备份保存在本机 .runtime/updates 目录。')
            else:
                if stopped:
                    try:
                        start_server(old_python, port)
                    except Exception:
                        pass
                runtime('update-maintenance').unlink(missing_ok=True)
                state('failed', busy=False, error=message, message='未替换程序。原有数据保留。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'check', 'launch', 'apply', 'recover'])
    args = parser.parse_args()
    import local_server
    local_server.load_environment()
    if args.action == 'launch':
        child = subprocess.Popen([sys.executable, str(ROOT / 'scripts/system_update.py'), 'apply'], cwd=ROOT, stdin=subprocess.DEVNULL, **detached_popen_kwargs())
        state('starting', process=process_identity(child.pid))
    elif args.action == 'apply':
        apply_update()
    elif args.action == 'recover':
        with FileLock(runtime('update-worker.lock')):
            journal = read_json(runtime('update-journal.json'))
            if not journal:
                raise UpdateError('没有需要恢复的更新。')
            restore(journal)
            state('rolled_back', busy=False, error='', message='已恢复旧版本。')
    else:
        print(json.dumps(status() if args.action == 'status' else check(), ensure_ascii=False))


if __name__ == '__main__':
    main()
