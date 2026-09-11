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
ROOT_FILES = {'manage.py', 'requirements.txt', 'README.md', 'system_version.json'}
EXCLUDED_PARTS = {'__pycache__', 'node_modules', '.venv', 'storage', 'uploads', 'logs', 'offline'}


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(repo), *args])


def allowed_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        return False
    if any(part.startswith('.') or part in EXCLUDED_PARTS for part in path.parts):
        return False
    if path.suffix.lower() in {'.pyc', '.pyo', '.sqlite', '.sqlite3', '.db', '.exe', '.msi', '.xlsx', '.xlsm'}:
        return False
    if len(path.parts) == 1:
        return name in ROOT_FILES or name == 'requirements-update.lock' or path.suffix.lower() in {'.bat', '.command'}
    return path.parts[0] in CODE_ROOTS


def build_release(repo: Path, output: Path, notes: str = '') -> dict:
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
    if not ROOT_FILES.issubset(files):
        raise ValueError('Git HEAD is missing required application root files')
    manifest = {
        'schema': 1, 'version': version, 'notes': notes,
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }
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
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New budget-system-update.zip path')
    parser.add_argument('--notes', default='', help='User-facing release notes')
    args = parser.parse_args()
    manifest = build_release(Path(__file__).resolve().parents[1], args.output, args.notes)
    print(f"Built {args.output}: version {manifest['version']}, {len(manifest['files'])} files")


if __name__ == '__main__':
    main()
