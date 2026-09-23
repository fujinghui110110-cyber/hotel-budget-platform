#!/usr/bin/env python3
"""Build the reviewed Git HEAD into a data-free system update archive."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

CODE_ROOTS = {'budgeting', 'config', 'scripts', 'templates', 'static', 'docs'}
ROOT_FILES = {'manage.py', 'requirements.txt', 'README.md', 'system_version.json',
              'requirements-update.lock', '一键启动.command', '一键启动-Windows.bat'}
REQUIRED_RUNTIME_FILES = {
    'config/settings.py', 'config/urls.py', 'config/wsgi.py',
    'budgeting/apps.py', 'scripts/local_server.py', 'scripts/system_update.py',
    'scripts/setup_mac.sh', 'scripts/setup_windows.ps1',
}
EXCLUDED_PARTS = {'__pycache__', 'node_modules', '.venv', 'storage', 'uploads', 'logs', 'offline'}


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(repo), *args])


def allowed_path(name: str) -> bool:
    path = PurePosixPath(name)
    if not name or ':' in name or path.is_absolute() or '..' in path.parts or '\\' in name:
        return False
    if any(part.startswith('.') or part in EXCLUDED_PARTS for part in name.split('/')):
        return False
    if any(part.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL',
            *('COM' + str(n) for n in range(1, 10)),
            *('LPT' + str(n) for n in range(1, 10))}
            or part.endswith((' ', '.')) or not part for part in name.split('/')):
        return False
    if path.suffix.lower() in {'.pyc', '.pyo', '.sqlite', '.sqlite3', '.db', '.exe', '.msi', '.xlsx', '.xlsm'}:
        return False
    if len(path.parts) == 1:
        return name in ROOT_FILES or name == 'requirements-update.lock' or path.suffix.lower() in {'.bat', '.command'}
    return path.parts[0] in CODE_ROOTS


def build_release(repo: Path, output: Path, notes: str = '', policy_path: Path | None = None) -> dict:
    commit = git(repo, 'rev-parse', 'HEAD').decode().strip()
    version_info = json.loads(git(repo, 'show', f'{commit}:system_version.json'))
    version = version_info['version']
    if not isinstance(version, str) or not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}\.\d+', version):
        raise ValueError('system_version.json version must look like 2026.09.11.1')
    files = {}
    entries = git(repo, 'ls-tree', '-rz', '--full-tree', commit).split(b'\0')
    for entry in entries:
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, object_id = metadata.decode().split()
        name = raw_name.decode('utf-8')
        if mode not in {'100644', '100755'} or kind != 'blob' or not allowed_path(name):
            continue
        files[name] = git(repo, 'cat-file', 'blob', object_id)
    if not (ROOT_FILES | REQUIRED_RUNTIME_FILES).issubset(files):
        raise ValueError('Git HEAD is missing required application root files')
    from scripts.release_manager import validate_manifest
    if policy_path is None:
        raise ValueError('必须提供经验证的 schema/capability 发布策略文件。')
    policy = json.loads(policy_path.read_text(encoding='utf-8'))
    manifest = {
        **policy,
        'schema': 2,
        'commit_sha': commit,
        'repository': 'fujinghui110110-cyber/hotel-budget-platform',
        'authenticity': 'github-attestation', 'version': version, 'notes': notes,
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }
    validate_manifest(manifest)
    committed_migrations = sorted(Path(name).stem for name in files if name.startswith('budgeting/migrations/') and Path(name).name[:1].isdigit())
    if committed_migrations != sorted(manifest['migration_ids']):
        raise ValueError('发布策略迁移清单与 Git HEAD 不一致，禁止发布未提交实现的能力。')
    if 'requirements-update.lock' not in files:
        raise ValueError('发布缺少带哈希的依赖锁文件。')
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f'Refusing to overwrite existing release: {output}')
    try:
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
        with zipfile.ZipFile(output) as archive:
            if archive.testzip():
                raise ValueError('Release ZIP integrity check failed')
    except Exception:
        output.unlink(missing_ok=True)
        raise
    asset_manifest = {**manifest, 'assets': [{'name': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'size': output.stat().st_size}]}
    output.with_name('release-manifest.json').write_text(json.dumps(asset_manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return asset_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New budget-system-update.zip path')
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--notes', default='', help='User-facing release notes')
    args = parser.parse_args()
    manifest = build_release(Path(__file__).resolve().parents[1], args.output, args.notes, args.policy)
    print(f"Built {args.output}: version {manifest['version']}, {len(manifest['files'])} files")


if __name__ == '__main__':
    main()
