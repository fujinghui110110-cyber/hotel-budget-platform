"""Build a real schema-1 updater bridge with the legacy business app unchanged."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from scripts.build_system_release import allowed_path, git

BASE_COMMIT = '48a933393e87f04d742c607dc9d955f5bb4e9531'
BRIDGE_VERSION = '2026.09.23.0'
OVERLAY = (
    'requirements-update.lock',
    'scripts/system_update.py', 'scripts/runtime_support.py',
    'scripts/release_adapter.py', 'scripts/release_manager.py',
    'scripts/build_system_release.py', 'scripts/github_cli.py',
    'scripts/bridge_bootstrap.py',
)


def build_bridge(repo, output):
    repo, output = Path(repo), Path(output)
    head = git(repo, 'rev-parse', 'HEAD').decode().strip()
    names = git(repo, 'ls-tree', '-rz', '--name-only', BASE_COMMIT).decode().split('\0')
    files = {name: git(repo, 'show', BASE_COMMIT + ':' + name) for name in names if name and allowed_path(name)}
    for name in OVERLAY:
        files[name] = git(repo, 'show', head + ':' + name)
    # Retain every original launcher action, with a verified-release handoff first.
    launcher = files['scripts/local_server.py'].decode()
    anchor = 'def main():\n'
    if launcher.count(anchor) != 1:
        raise ValueError('Legacy launcher entry point changed; bridge needs fresh review')
    launcher = launcher.replace(anchor, anchor + "    sys.path.insert(0, str(ROOT))\n    from scripts.bridge_bootstrap import dispatch_active_release\n    load_environment()\n    dispatch_active_release(ROOT)\n")
    import_line = next(line for line in launcher.splitlines() if line.startswith('from runtime_support import '))
    launcher = launcher.replace(import_line, 'try:\n    ' + import_line.replace('from runtime_support', 'from scripts.runtime_support') + '\nexcept ImportError:\n    ' + import_line)
    files['scripts/local_server.py'] = launcher.encode()
    files['system_version.json'] = (json.dumps({'version': BRIDGE_VERSION}) + '\n').encode()
    manifest = {
        'schema': 1, 'version': BRIDGE_VERSION, 'commit_sha': head,
        'bridge_only': True, 'base_commit': BASE_COMMIT,
        'database_migrations_changed': False,
        'notes': '在线升级组件已准备好。请再次检查更新，安装新版预算系统。',
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = build_bridge(args.repo, args.output)
    print('Built legacy updater bridge ' + manifest['version'])


if __name__ == '__main__':
    main()
