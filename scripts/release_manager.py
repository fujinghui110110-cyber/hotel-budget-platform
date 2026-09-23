"""Fail-closed immutable releases. Data restoration is deliberately not code rollback."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile

REPOSITORY = 'fujinghui110110-cyber/hotel-budget-platform'
CAPABILITIES = {'annual-targets-v1', 'locked-history-v1'}


class ReleaseError(ValueError):
    pass


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def validate_manifest(manifest):
    if manifest.get('schema') != 2 or not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}\.\d+', manifest.get('version', '')):
        raise ReleaseError('缺少正式版本或新版发布清单。')
    if not re.fullmatch(r'[0-9a-f]{40}', manifest.get('commit_sha', '')) or len(set(manifest['commit_sha'])) == 1:
        raise ReleaseError('发布清单缺少真实 commit。')
    if manifest.get('repository') != REPOSITORY or manifest.get('authenticity') not in {'github-attestation', 'local-admin-registration'}:
        raise ReleaseError('发布来源或真实性策略不受信任。')
    if manifest.get('authenticity') == 'local-admin-registration' and manifest.get('local_registration') is not True:
        raise ReleaseError('缺少本机管理员登记标记。')
    for key in ('database_schema_read_range', 'database_schema_write_range'):
        bounds = manifest.get(key)
        if not isinstance(bounds, list) or len(bounds) != 2 or any(type(v) is not int or v < 0 for v in bounds) or bounds[0] > bounds[1]:
            raise ReleaseError('缺少已验证 schema 范围。')
    capabilities = manifest.get('capabilities', [])
    if not isinstance(capabilities, list) or not CAPABILITIES <= set(capabilities):
        raise ReleaseError('程序不支持历史锁和年度目标。')
    if manifest.get('rollback') != {'mode': 'code_only_when_compatible'}:
        raise ReleaseError('不支持自动数据库恢复式回退。')
    if not manifest.get('migration_ids') or not manifest.get('supported_python'):
        raise ReleaseError('发布清单缺少迁移或运行时清单。')
    return manifest


def verify_attestation(archive, manifest, token=None):
    """GitHub CLI verifies signed provenance; the archive embeds the manifest."""
    validate_manifest(manifest)
    if manifest['authenticity'] != 'github-attestation':
        raise ReleaseError('本机登记不能作为远程升级来源。')
    gh = shutil.which('gh')
    if not gh:
        raise ReleaseError('需要 GitHub CLI 验证发布证明，未安装时禁止升级。')
    verification_env = os.environ.copy()
    if token:
        verification_env['GH_TOKEN'] = token
    result = subprocess.run([gh, 'attestation', 'verify', str(archive), '--repo', REPOSITORY,
                             '--signer-workflow', REPOSITORY + '/.github/workflows/release.yml',
                             '--source-digest', manifest['commit_sha'], '--deny-self-hosted-runners'],
                            capture_output=True, timeout=120, env=verification_env)
    if result.returncode:
        raise ReleaseError('GitHub 发布证明验证失败，未安装任何代码。')


def compatible(manifest, schema, required_capabilities):
    validate_manifest(manifest)
    for key in ('database_schema_read_range', 'database_schema_write_range'):
        low, high = manifest[key]
        if not low <= schema <= high:
            raise ReleaseError('当前数据库 schema 与程序不兼容，禁止回退或升级。')
    if not set(required_capabilities) <= set(manifest['capabilities']):
        raise ReleaseError('程序缺少当前数据要求的语义能力。')


def upgrade_compatible(manifest, schema, required_capabilities):
    validate_manifest(manifest)
    bounds = manifest.get('upgrade_from_schema_range', manifest['database_schema_write_range'])
    if not isinstance(bounds, list) or len(bounds) != 2 or any(type(v) is not int for v in bounds) or not bounds[0] <= schema <= bounds[1]:
        raise ReleaseError('该数据库没有经过验证的迁移路径。')
    if not set(required_capabilities) <= set(manifest['capabilities']):
        raise ReleaseError('新版缺少当前业务所需能力。')


def files_digest(folder):
    folder = Path(folder)
    found = {}
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():
            raise ReleaseError('数据目录含符号链接，无法保证完整备份。')
        if path.is_file():
            found[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def backup_data(database, roots, destination):
    """Caller must first quiesce every writer. Includes originals/templates/config."""
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Confirm no writer holds a lock before the backup API, which otherwise may wait indefinitely.
    with closing(sqlite3.connect(str(database), timeout=3)) as probe:
        probe.execute('BEGIN IMMEDIATE')
        probe.rollback()
    with closing(sqlite3.connect(str(database), timeout=3)) as source, closing(sqlite3.connect(str(destination / 'database.sqlite3'))) as target:
        source.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ReleaseError('备份数据库完整性校验失败。')
    for label, source in roots.items():
        if label not in {'storage', 'templates', 'config'}:
            raise ReleaseError('未知备份范围。')
        source = Path(source)
        if not source.is_dir() or source.is_symlink():
            raise ReleaseError('备份范围不存在或不安全。')
        before = files_digest(source)
        shutil.copytree(source, destination / label)
        if before != files_digest(destination / label) or before != files_digest(source):
            raise ReleaseError('备份期间源文件变动或副本不完整。')
    if set(roots) != {'storage', 'templates', 'config'}:
        raise ReleaseError('备份必须包括原件、历史模板和配置。')
    atomic_json(destination / 'backup-manifest.json', files_digest(destination))


class ReleaseManager:
    def __init__(self, install_root, data_root):
        self.install = Path(install_root).resolve()
        self.data = Path(data_root).resolve()
        self.pointer = self.install / 'active-release.json'
        self.journal = self.data / 'update-journal.json'

    def phase(self, name, **details):
        previous = read_json(self.journal) if self.journal.exists() else {}
        previous.update(details)
        previous['phase'] = name
        previous.setdefault('events', []).append(name)
        atomic_json(self.journal, previous)

    def pointer_for(self, release):
        release = Path(release).resolve()
        if release.parent != (self.install / 'releases').resolve():
            raise ReleaseError('版本不在独立 releases 目录中。')
        if (release / 'registration-failed.json').exists() or not (release / 'runtime-validated.json').is_file():
            raise ReleaseError('版本运行时尚未完成依赖验证，禁止激活。')
        manifest = validate_manifest(read_json(release / 'manifest.json'))
        python = release / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.is_file():
            raise ReleaseError('版本缺少独立 Python 运行时。')
        return {'schema': 1, 'version': manifest['version'], 'commit_sha': manifest['commit_sha'],
                'release_dir': str(release), 'python': str(python)}

    def rollback_code(self, release, *, schema, required_capabilities, quiesce, healthcheck):
        """Never open, restore or replace the business DB during code rollback."""
        if self.journal.exists() and read_json(self.journal).get('phase') not in {'COMPLETE', 'ROLLBACK_COMPLETE'}:
            raise ReleaseError('存在未完成的迁移，禁止直接回退代码。')
        pointer = self.pointer_for(release)
        compatible(read_json(Path(release) / 'manifest.json'), schema, required_capabilities)
        prior = read_json(self.pointer)
        self.phase('ROLLBACK_CHECK', prior=prior, candidate=pointer)
        quiesce()
        self.phase('ROLLBACK_ACTIVATE')
        atomic_json(self.pointer, pointer)
        try:
            healthcheck(pointer)
        except Exception:
            atomic_json(self.pointer, prior)
            self.phase('ROLLBACK_FAILED')
            raise
        self.phase('ROLLBACK_COMPLETE')
        return pointer

    def install_release(self, release, *, database, roots, schema, required_capabilities,
                        quiesce, migrate, reconcile, healthcheck, verify):
        """Staged release must already have its independent, hash-locked runtime.

        Callbacks are platform adapters. verify MUST verify authenticated artifact;
        migrate receives explicit DB path, never an implicit production setting.
        """
        if self.journal.exists() and read_json(self.journal).get('phase') not in {'COMPLETE', 'ROLLBACK_COMPLETE', 'FAILED'}:
            raise ReleaseError('上次更新未完成，需要管理员检查恢复记录。')
        pointer = self.pointer_for(release)
        manifest = read_json(Path(release) / 'manifest.json')
        upgrade_compatible(manifest, schema, required_capabilities)
        previous = read_json(self.pointer) if self.pointer.exists() else None
        self.phase('CHECK', prior=previous, candidate=pointer)
        try:
            verify()
            self.phase('VERIFY')
            quiesce()
            self.phase('QUIESCE')
            backup = self.data / 'backups' / manifest['version']
            backup_data(database, roots, backup)
            self.phase('BACKUP', backup=str(backup))
            trial = backup.parent / (manifest['version'] + '-trial.sqlite3')
            shutil.copy2(backup / 'database.sqlite3', trial)
            self.phase('STAGE')
            migrate(pointer, trial)
            reconcile(backup / 'database.sqlite3', trial)
            self.phase('MIGRATE')
            migrate(pointer, Path(database))
            reconcile(backup / 'database.sqlite3', Path(database))
            self.phase('VALIDATE')
            atomic_json(self.pointer, pointer)
            self.phase('ACTIVATE')
            healthcheck(pointer)
            self.phase('COMPLETE')
        except Exception:
            # Never restore old DB: writes may have happened or migration may be partial.
            # Keep maintenance gate closed; explicit recovery checks compatibility.
            self.phase('RECOVERY_REQUIRED')
            raise
        return pointer
