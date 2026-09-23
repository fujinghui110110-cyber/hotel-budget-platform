"""Data-free release payload and clean-directory runtime regression checks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from scripts import build_system_release as builder
from scripts import system_update
from tests.test_release_manager import manifest


class ReleasePackagingTests(unittest.TestCase):
    def test_builder_and_updater_share_path_policy(self):
        self.assertEqual(builder.CODE_ROOTS, system_update.CODE_DIRS)
        for name in ('一键启动.command', '一键启动-Windows.bat',
                     'config/settings.py', 'requirements-update.lock'):
            self.assertTrue(builder.allowed_path(name), name)
            self.assertTrue(system_update.allowed_path(name), name)
        for name in ('hotel_budget/settings.py', 'storage/business.xlsx', 'config/.env', 'scripts/logs/private.txt',
                     'docs/source.xlsm', 'db.sqlite3', 'config/private.db',
                     '../config/settings.py', 'config/../settings.py', '/manage.py',
                     'config\\settings.py', 'config/CON.txt', 'scripts/tool.py:secret',
                     'config//settings.py', 'config/settings.py.', ''):
            self.assertFalse(builder.allowed_path(name), name)
            self.assertFalse(system_update.allowed_path(name), name)

    def test_archive_starts_from_clean_directory(self):
        source = Path(__file__).resolve().parents[1]
        files = {}
        candidates = list(source.iterdir())
        for root in builder.CODE_ROOTS:
            candidates.extend((source / root).rglob('*'))
        for path in candidates:
            name = path.relative_to(source).as_posix()
            if path.is_file() and not path.is_symlink() and builder.allowed_path(name):
                files[name] = path.read_bytes()
        version = json.loads(files['system_version.json'])['version']
        policy = manifest(version)
        policy['migration_ids'] = sorted(Path(name).stem for name in files
            if name.startswith('budgeting/migrations/') and Path(name).name[:1].isdigit())
        objects = {hashlib.sha256(data).hexdigest(): data for data in files.values()}
        tree = b'\0'.join(('100644 blob ' + hashlib.sha256(data).hexdigest() + '\t' + name).encode()
                          for name, data in files.items()) + b'\0'

        def snapshot_git(repo, *args):
            if args == ('rev-parse', 'HEAD'):
                return policy['commit_sha'].encode()
            if args[0] == 'show':
                return files['system_version.json']
            if args[0] == 'ls-tree':
                return tree
            if args[0] == 'cat-file':
                return objects[args[-1]]
            raise AssertionError(args)

        with tempfile.TemporaryDirectory(prefix='budget-release-') as directory:
            root = Path(directory)
            policy_path = root / 'policy.json'
            policy_path.write_text(json.dumps(policy))
            archive = root / 'update.zip'
            with mock.patch.object(builder, 'git', side_effect=snapshot_git):
                result = builder.build_release(source, archive, policy_path=policy_path)
            extracted = root / 'clean'
            extracted.mkdir()
            system_update.validate_archive(archive, extracted, version)
            if os.name != 'nt':
                self.assertTrue((extracted / '一键启动.command').stat().st_mode & 0o111)
            else:
                self.assertTrue((extracted / '一键启动-Windows.bat').is_file())
            self.assertNotIn('hotel_budget/settings.py', result['files'])
            self.assertFalse((extracted / 'db.sqlite3').exists())
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('DJANGO_', 'BUDGET_', 'PYTHONPATH'))}
            env.update(DJANGO_SETTINGS_MODULE='config.settings', DJANGO_DEBUG='1',
                       DATABASE_PATH=str(root / 'isolated.sqlite3'),
                       BUDGET_STORAGE_ROOT=str(root / 'storage'))
            checked = subprocess.run([sys.executable, 'manage.py', 'check'],
                cwd=extracted, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            imported = subprocess.run([sys.executable, '-c',
                'import config.wsgi; '
                'assert config.wsgi.application'], cwd=extracted, env=env,
                text=True, capture_output=True, timeout=60)
            self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
            launcher = subprocess.run([sys.executable, 'scripts/local_server.py', '--help'],
                cwd=extracted, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(launcher.returncode, 0, launcher.stdout + launcher.stderr)
            self.assertFalse((source / 'isolated.sqlite3').exists())
