import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_legacy_bridge import BASE_COMMIT, BRIDGE_VERSION, build_bridge
from scripts.bridge_bootstrap import dispatch_active_release


class LegacyBridgeTests(unittest.TestCase):
    def test_original_validator_accepts_bridge_and_business_schema_is_unchanged(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='budget bridge ') as temporary:
            root = Path(temporary)
            archive = root / 'bridge.zip'
            manifest = build_bridge(repo, archive)
            legacy_file = root / 'old_updater.py'
            legacy_file.write_bytes(subprocess.check_output(['git', '-C', str(repo), 'show', BASE_COMMIT + ':scripts/system_update.py']))
            spec = importlib.util.spec_from_file_location('legacy_bridge_validator', legacy_file)
            legacy = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(legacy)
            target = root / 'installed'
            legacy.validate_archive(archive, target, BRIDGE_VERSION)
            protected = [name for name in manifest['files'] if name.startswith(('budgeting/', 'config/', 'templates/', 'static/')) or name == 'requirements.txt']
            self.assertGreater(len(protected), 100)
            for name in protected:
                expected = subprocess.check_output(['git', '-C', str(repo), 'show', BASE_COMMIT + ':' + name])
                self.assertEqual((target / name).read_bytes(), expected, name)
            self.assertEqual(manifest['schema'], 1)
            self.assertFalse(manifest['database_migrations_changed'])
            self.assertIn('requirements-update.lock', manifest['files'])
            self.assertIn('--hash=sha256:', (target / 'requirements-update.lock').read_text(encoding='utf-8'))

    def test_original_shortcut_keeps_shared_data_paths_after_handoff(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary).resolve()
            release = root / 'releases' / '2026.09.23.1'
            python = release / '.venv' / 'python'
            python.parent.mkdir(parents=True)
            python.touch()
            launcher = release / 'scripts' / 'local_server.py'
            launcher.parent.mkdir()
            launcher.touch()
            (root / 'active-release.json').write_text(json.dumps({'schema': 1, 'release_dir': str(release), 'python': str(python)}))
            with patch('scripts.bridge_bootstrap.os.execv') as execute:
                dispatch_active_release(root)
            self.assertEqual(os.environ['DATABASE_PATH'], str(root / 'db.sqlite3'))
            self.assertEqual(os.environ['BUDGET_STORAGE_ROOT'], str(root / 'storage'))
            self.assertEqual(os.environ['BUDGET_TEMPLATE_ROOT'], str(root))
            self.assertEqual(execute.call_args.args[0], str(python))
