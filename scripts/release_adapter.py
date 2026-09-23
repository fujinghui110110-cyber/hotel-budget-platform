"""Local update adapter. All subprocesses receive explicit shared-data paths."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile

from scripts.release_manager import ReleaseManager, ReleaseError, CAPABILITIES, validate_manifest, compatible, upgrade_compatible, verify_attestation, atomic_json


def database_schema(database):
    with closing(sqlite3.connect(f'file:{Path(database).resolve()}?mode=ro', uri=True)) as db:
        rows = db.execute("SELECT name FROM django_migrations WHERE app='budgeting'").fetchall()
    return max(int(row[0].split('_')[0]) for row in rows)


def reconcile_database(before, after):
    """Every old business row and column must survive additive migration unchanged."""
    with closing(sqlite3.connect(before)) as old, closing(sqlite3.connect(after)) as new:
        tables = [row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'budgeting_%'")]
        for table in tables:
            if not table.replace('_', '').isalnum():
                raise ReleaseError('数据库表名无效。')
            columns = [row[1] for row in old.execute(f'PRAGMA table_info("{table}")')]
            fields = ','.join('"' + column.replace('"', '""') + '"' for column in columns)
            a = old.execute(f'SELECT {fields} FROM "{table}" ORDER BY id').fetchall()
            b = new.execute(f'SELECT {fields} FROM "{table}" ORDER BY id').fetchall()
            if a != b:
                raise ReleaseError('升级前后业务数据不一致，维护模式保持开启。')


def environment(update):
    from scripts import local_server
    local_server.load_environment()
    env = os.environ.copy()
    env['DATABASE_PATH'] = str(update.settings_paths().resolve())
    env['BUDGET_STORAGE_ROOT'] = str(Path(env.get('BUDGET_STORAGE_ROOT', update.ROOT / 'storage')).resolve())
    env['BUDGET_RUNTIME_ROOT'] = str(update.runtime('').resolve())
    env['BUDGET_LOG_ROOT'] = str(Path(env.get('BUDGET_LOG_ROOT', update.ROOT / 'logs')).resolve())
    env['BUDGET_INSTALL_ROOT'] = str(Path(env.get('BUDGET_INSTALL_ROOT', update.ROOT)).resolve())
    env['BUDGET_TEMPLATE_ROOT'] = str(Path(env.get('BUDGET_TEMPLATE_ROOT', env['BUDGET_INSTALL_ROOT'])).resolve())
    return env


def execute(command, cwd, env, timeout=300):
    result = subprocess.run([str(v) for v in command], cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ReleaseError('升级子进程失败，维护模式保持开启。')
    return result.stdout


def adapters(update, env):
    port = update.read_json(update.runtime('server.json')).get('port', 8768)
    def quiesce():
        update.runtime('update-maintenance').write_text('updating', encoding='utf-8')
        from scripts.runtime_support import identity_matches
        server = update.read_json(update.runtime('server.json'))
        execute([sys.executable, update.ROOT / 'scripts/stop_all.py'], update.ROOT, env, 330)
        identities = [server.get('supervisor_identity'), *server.get('children_identity', [])]
        if any(identity_matches(identity) for identity in identities if identity):
            raise ReleaseError('仍有写入进程，禁止迁移数据库。')
    def migrate(pointer, database):
        trial_env = {**env, 'DATABASE_PATH': str(database)}
        execute([pointer['python'], Path(pointer['release_dir']) / 'manage.py', 'migrate', '--noinput'], pointer['release_dir'], trial_env)
        compatible(json.loads((Path(pointer['release_dir']) / 'manifest.json').read_text()), database_schema(database), CAPABILITIES)
    def healthcheck(pointer):
        execute([pointer['python'], Path(pointer['release_dir']) / 'scripts/local_server.py', 'start', '--port', port], pointer['release_dir'], env, 180)
        import urllib.request
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=10) as response:
            payload = json.loads(response.read(65536))
            if response.status != 200 or not payload.get('database') or not payload.get('storage'):
                raise ReleaseError('新版健康检查失败。')
    return quiesce, migrate, healthcheck


def apply_update(update):
    env = environment(update)
    manager = ReleaseManager(env['BUDGET_INSTALL_ROOT'], Path(env['DATABASE_PATH']).parent)
    with update.FileLock(update.runtime('update-worker.lock')):
        release = update.read_json(update.runtime('update-release.json'))
        operation = update.runtime('updates') / (str(time.time_ns()))
        operation.mkdir(parents=True, mode=0o700)
        try:
            required_space = Path(env['DATABASE_PATH']).stat().st_size * 4 + update.MAX_ARCHIVE * 4
            if shutil.disk_usage(operation).free < required_space:
                raise ReleaseError('可用磁盘空间不足，未开始升级。')
            update.state('downloading', busy=True, message='正在验证正式发布来源并准备独立版本。')
            archive = operation / 'update.zip'
            digest = hashlib.sha256()
            size = 0
            with update.github_open(update.API + '/releases/assets/' + str(release['asset_id']), 'application/octet-stream') as source, archive.open('wb') as target:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > update.MAX_ARCHIVE:
                        raise ReleaseError('更新文件超过大小限制。')
                    digest.update(chunk)
                    target.write(chunk)
            if digest.hexdigest() != release['sha256'] or size != release['size']:
                raise ReleaseError('更新文件校验失败。')
            with zipfile.ZipFile(archive) as package:
                item = package.getinfo('manifest.json')
                if item.file_size > 2 * 1024 * 1024:
                    raise ReleaseError('发布清单过大。')
                manifest = validate_manifest(json.loads(package.read(item)))
            token = update.read_json(update.runtime('update-config.json')).get('token')
            verify_attestation(archive, manifest, token=token)
            if f'{sys.version_info.major}.{sys.version_info.minor}' not in manifest['supported_python']:
                raise ReleaseError('当前 Python 不在已验证运行时范围。')
            upgrade_compatible(manifest, database_schema(env['DATABASE_PATH']), CAPABILITIES)
            destination = manager.install / 'releases' / release['version']
            if destination.exists():
                raise ReleaseError('该不可变版本目录已存在，请检查上次升级记录。')
            destination.mkdir(parents=True)
            update.validate_archive(archive, destination, release['version'])
            atomic_json(destination / 'manifest.json', manifest)
            lock = destination / 'requirements-update.lock'
            if not lock.is_file():
                raise ReleaseError('发布包缺少哈希锁定依赖。')
            execute([sys.executable, '-m', 'venv', destination / '.venv'], destination, env)
            python = update.python_in(destination / '.venv')
            execute([python, '-m', 'pip', 'install', '--only-binary=:all:', '--require-hashes', '-r', lock, '-r', destination / 'requirements.txt'], destination, env, 900)
            execute([python, '-m', 'pip', 'check'], destination, env)
            atomic_json(destination / 'runtime-validated.json', {'pip_check': True, 'locked_dependencies': True})
            config_copy = operation / 'config'
            config_copy.mkdir(mode=0o700)
            for name in ('.env', '.env.production'):
                if (update.ROOT / name).exists():
                    shutil.copy2(update.ROOT / name, config_copy / name)
            # Effective settings preserve separate environment-based installations too.
            atomic_json(config_copy / 'paths.json', {k: v for k, v in env.items() if k in {'DATABASE_PATH', 'BUDGET_STORAGE_ROOT', 'BUDGET_RUNTIME_ROOT', 'BUDGET_INSTALL_ROOT'}})
            private_config = {k: v for k, v in env.items() if k.startswith(('DJANGO_', 'BUDGET_', 'DATABASE_', 'CSRF_', 'SESSION_', 'SECURE_'))}
            atomic_json(config_copy / 'effective-settings.json', private_config)
            (config_copy / 'effective-settings.json').chmod(0o600)
            storage = Path(env['BUDGET_STORAGE_ROOT'])
            templates = operation / 'referenced-templates'
            templates.mkdir()
            template_index = []
            with closing(sqlite3.connect(f"file:{Path(env['DATABASE_PATH']).resolve()}?mode=ro", uri=True)) as db:
                template_rows = db.execute('SELECT id, file_path, manifest_path FROM budgeting_templateversion').fetchall()
            for template_id, workbook, catalog in template_rows:
                for kind, raw in (('workbook', workbook), ('manifest', catalog)):
                    source = Path(raw)
                    if not source.is_absolute():
                        source = Path(env['BUDGET_TEMPLATE_ROOT']) / source
                    if not source.is_file() or source.is_symlink():
                        raise ReleaseError('数据库引用的历史模板文件缺失，禁止升级。')
                    target = templates / (str(template_id) + '-' + kind + source.suffix)
                    shutil.copy2(source, target)
                    template_index.append({'source': str(source), 'backup': target.name})
            atomic_json(templates / 'sources.json', template_index)
            quiesce, migrate, healthcheck = adapters(update, env)
            manager.install_release(destination, database=env['DATABASE_PATH'], roots={'storage': storage, 'templates': templates, 'config': config_copy},
                schema=database_schema(env['DATABASE_PATH']), required_capabilities=CAPABILITIES, quiesce=quiesce, migrate=migrate,
                reconcile=reconcile_database, healthcheck=healthcheck, verify=lambda: verify_attestation(archive, manifest, token=token))
            update.runtime('update-maintenance').unlink(missing_ok=True)
            update.state('completed', busy=False, error='', message='更新完成，原件、历史模板和旧版本均已保留。')
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ReleaseError, update.UpdateError)) else '更新未完成，请检查升级日志与备份状态。'
            update.state('recovery_required' if update.runtime('update-maintenance').exists() else 'failed', busy=False, error=message)


def rollback(update, version):
    env = environment(update)
    manager = ReleaseManager(env['BUDGET_INSTALL_ROOT'], Path(env['DATABASE_PATH']).parent)
    update.version_key(version)
    with update.FileLock(update.runtime('update-worker.lock')):
        quiesce, _, healthcheck = adapters(update, env)
        manager.rollback_code(manager.install / 'releases' / version, schema=database_schema(env['DATABASE_PATH']),
                              required_capabilities=CAPABILITIES, quiesce=quiesce, healthcheck=healthcheck)
        update.runtime('update-maintenance').unlink(missing_ok=True)
        update.state('completed', busy=False, error='', message='已切回兼容代码版本，当前业务数据库保持不变。')
    return update.status()
