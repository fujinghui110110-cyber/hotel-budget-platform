"""Explicitly register reviewed local code in an isolated runtime, without moving data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from scripts.build_system_release import allowed_path, CODE_ROOTS
from scripts.release_manager import ReleaseManager, ReleaseError, atomic_json, validate_manifest, compatible, CAPABILITIES


def register(source, install, policy_path, *, wheelhouse=None, activate=False, database=None):
    source, install = Path(source).resolve(), Path(install).resolve()
    policy = json.loads(Path(policy_path).read_text())
    version = json.loads((source / 'system_version.json').read_text())['version']
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    manifest = {**policy, 'schema': 2, 'version': version, 'commit_sha': commit,
                'repository': 'fujinghui110110-cyber/hotel-budget-platform',
                'authenticity': 'local-admin-registration', 'local_registration': True}
    validate_manifest(manifest)
    migrations = sorted(p.stem for p in (source / 'budgeting/migrations').glob('[0-9]*.py'))
    if migrations != sorted(manifest['migration_ids']):
        raise ReleaseError('本地实现与经核验策略的迁移清单不同。')
    if not (source / 'requirements-update.lock').is_file():
        raise ReleaseError('本地登记必须具备哈希依赖锁。')
    target = install / 'releases' / version
    if target.exists():
        raise ReleaseError('版本目录已存在，禁止覆盖。')
    target.mkdir(parents=True)
    files = {}
    try:
        candidates = list(source.iterdir())
        for name in CODE_ROOTS:
            if (source / name).is_dir():
                candidates.extend((source / name).rglob('*'))
        for path in candidates:
            if path.is_symlink():
                continue
            relative = path.relative_to(source).as_posix()
            if not path.is_file() or not allowed_path(relative):
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            files[relative] = hashlib.sha256(destination.read_bytes()).hexdigest()
        manifest['files'] = files
        atomic_json(target / 'manifest.json', manifest)
        subprocess.run([sys.executable, '-m', 'venv', str(target / '.venv')], check=True, capture_output=True)
        python = target / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        command = [str(python), '-m', 'pip', 'install', '--only-binary=:all:', '--require-hashes', '-r', str(target / 'requirements-update.lock'), '-r', str(target / 'requirements.txt')]
        if wheelhouse:
            command += ['--no-index', '--find-links', str(Path(wheelhouse).resolve())]
        subprocess.run(command, check=True, capture_output=True, timeout=900)
        subprocess.run([str(python), '-m', 'pip', 'check'], check=True, capture_output=True)
        # Import/compile only: never migrate or create business DB during registration.
        subprocess.run([str(python), '-m', 'compileall', '-q', str(target / 'budgeting'), str(target / 'scripts')], check=True)
        atomic_json(target / 'runtime-validated.json', {'pip_check': True, 'locked_dependencies': True})
        manager = ReleaseManager(install, install)
        pointer = manager.pointer_for(target)
        if activate:
            if database is None:
                raise ReleaseError('启用首次版本必须显式指定当前数据库，仅执行只读兼容检查。')
            from scripts.release_adapter import database_schema
            compatible(manifest, database_schema(database), CAPABILITIES)
            if manager.pointer.exists():
                raise ReleaseError('已有 active 指针，不允许首次登记覆盖。')
            atomic_json(manager.pointer, pointer)
        atomic_json(target / 'registration.json', {'source_root': str(source), 'activated': activate,
                    'data_moved': False, 'database_migrated': False, 'source_content_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()})
        return pointer
    except Exception:
        # Incomplete candidate stays clearly marked; never remove original or data.
        atomic_json(target / 'registration-failed.json', {'status': 'FAILED', 'data_moved': False})
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--install', type=Path, required=True)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--database', type=Path, help='Existing database for read-only activation compatibility check')
    parser.add_argument('--activate', action='store_true', help='Administrator explicitly enables the initial pointer; no DB migration.')
    args = parser.parse_args()
    print(json.dumps(register(args.source, args.install, args.policy, wheelhouse=args.wheelhouse, activate=args.activate, database=args.database), ensure_ascii=False))
