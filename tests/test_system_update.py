import hashlib
import io
import json
from pathlib import Path
import tempfile
from unittest import mock
import zipfile

from django.test import SimpleTestCase, RequestFactory, override_settings
from scripts import system_update as update
from budgeting.update_middleware import UpdateMaintenanceMiddleware


class SystemUpdateTests(SimpleTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.patch = mock.patch.object(update, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        (self.root / 'system_version.json').write_text('{"version":"2026.09.11.1"}')

    def archive(self, changes=None):
        files = {'manage.py': b'# manage', 'scripts/local_server.py': b'# local',
                 'scripts/system_update.py': b'# update', 'requirements.txt': b'Django==5.2.6',
                 'system_version.json': b'{"version":"2026.09.11.2"}'}
        files.update(changes or {})
        manifest = {'schema': 1, 'version': '2026.09.11.2',
                    'files': {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
        result = io.BytesIO()
        with zipfile.ZipFile(result, 'w') as z:
            z.writestr('manifest.json', json.dumps(manifest))
            for n, d in files.items():
                z.writestr(n, d)
        result.seek(0)
        return result

    def test_release_only_accepts_verified_stable_asset(self):
        release = {'tag_name': 'v2026.09.11.2', 'body': '新版本', 'assets': [
            {'name': update.ASSET_NAME, 'id': 1, 'size': 20, 'digest': 'sha256:' + 'a' * 64}]}
        with mock.patch.object(update, 'github_open', return_value=io.BytesIO(json.dumps(release).encode())):
            self.assertTrue(update.check()['update_available'])
        release['prerelease'] = True
        with mock.patch.object(update, 'github_open', return_value=io.BytesIO(json.dumps(release).encode())):
            with self.assertRaises(ValueError):
                update.check()

    def test_archive_rejects_data_paths_and_traversal(self):
        for path in ('../db.sqlite3', 'storage/budget.xlsx', '.env', 'scripts/../../x', 'scripts\\x.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                update.validate_archive(self.archive({path: b'bad'}), self.root / 'candidate', '2026.09.11.2')
        update.validate_archive(self.archive(), self.root / 'good', '2026.09.11.2')
        self.assertTrue((self.root / 'good/manage.py').is_file())

    def test_archive_rejects_hash_mismatch_and_version_mismatch(self):
        with self.assertRaises(ValueError):
            update.validate_archive(self.archive(), self.root / 'candidate', '2026.09.11.3')
        with self.assertRaises(ValueError):
            update.validate_archive(self.archive({'system_version.json': b'{"version":"2026.09.11.0"}'}), self.root / 'candidate', '2026.09.11.2')

    def test_credentials_never_appear_in_status(self):
        update.configure('github_pat_' + 'x' * 40)
        self.assertTrue(update.status()['configured'])
        self.assertNotIn('github_pat_', json.dumps(update.status()))
        with self.assertRaises(ValueError):
            update.configure('token\nInjected: header')

    def test_maintenance_blocks_budget_writes_but_health_is_available(self):
        update.runtime('update-maintenance').parent.mkdir(exist_ok=True)
        update.runtime('update-maintenance').touch()
        downstream = mock.Mock(return_value='health')
        middleware = UpdateMaintenanceMiddleware(downstream)
        with override_settings(BASE_DIR=self.root):
            self.assertEqual(middleware(RequestFactory().post('/uploads/')).status_code, 503)
            self.assertEqual(middleware(RequestFactory().get('/healthz')), 'health')
            self.assertEqual(middleware(RequestFactory().get('/management/update/status/')), 'health')
        self.assertEqual(downstream.call_count, 2)

    def test_backup_excludes_data_and_environment(self):
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/a.py').write_text('code')
        (self.root / 'storage').mkdir()
        (self.root / 'storage/a.xlsx').write_bytes(b'private')
        (self.root / '.env').write_text('secret')
        self.assertEqual(set(update.managed_files()), {'system_version.json', 'scripts/a.py'})

    def test_rollback_restores_database_code_and_interpreter(self):
        import sqlite3
        backup = self.root / 'backup'
        (backup / 'code').mkdir(parents=True)
        (backup / 'code/manage.py').write_text('old')
        (self.root / 'manage.py').write_text('new')
        db = self.root / 'db.sqlite3'
        with sqlite3.connect(db) as conn:
            conn.execute('create table sentinel(value text)')
            conn.execute("insert into sentinel values ('preserved')")
        with sqlite3.connect(db) as source, sqlite3.connect(backup / 'database.sqlite3') as target:
            source.backup(target)
        with sqlite3.connect(db) as conn:
            conn.execute('delete from sentinel')
        journal = {'backup': str(backup), 'old_files': ['manage.py'], 'new_files': ['manage.py'],
                   'database': str(db), 'database_existed': True, 'old_python_pointer': {},
                   'old_python': 'python', 'port': 8878}
        with mock.patch.object(update, 'run') as command, mock.patch.object(update, 'start_server'):
            update.restore(journal)
            command.assert_not_called()  # Recovery must not execute the failed candidate's shutdown code.
        self.assertEqual((self.root / 'manage.py').read_text(), 'old')
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute('select value from sentinel').fetchone()[0], 'preserved')

    def test_stale_launch_without_process_does_not_spin_forever(self):
        update.write_json(update.runtime('update-state.json'), {'busy': True, 'status': 'starting', 'process': None, 'updated_at': 1})
        self.assertFalse(update.status()['busy'])
        self.assertEqual(update.status()['status'], 'interrupted')

    def test_recent_launch_is_still_busy(self):
        import time
        update.write_json(update.runtime('update-state.json'), {'busy': True, 'status': 'starting', 'process': None, 'updated_at': time.time()})
        self.assertTrue(update.status()['busy'])
