from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.release_manager import ReleaseManager, ReleaseError, atomic_json, compatible, validate_manifest


def manifest(version):
    return {'schema': 2, 'version': version, 'commit_sha': '1234567890abcdef1234567890abcdef12345678',
            'repository': 'fujinghui110110-cyber/hotel-budget-platform', 'authenticity': 'github-attestation',
            'database_schema_read_range': [1, 2], 'database_schema_write_range': [1, 2],
            'capabilities': ['annual-targets-v1', 'locked-history-v1'],
            'rollback': {'mode': 'code_only_when_compatible'}, 'migration_ids': ['0001_initial'],
            'supported_python': ['3.13']}


class ReleaseManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='预算 空格 ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.db = self.data / 'db.sqlite3'
        with closing(sqlite3.connect(self.db)) as db:
            with db:
                db.execute('CREATE TABLE uploads (id INTEGER PRIMARY KEY, name TEXT)')
                db.execute("INSERT INTO uploads(name) VALUES ('原件')")
        self.roots = {}
        for label in ('storage', 'templates', 'config'):
            folder = self.data / label
            folder.mkdir()
            (folder / 'keep.txt').write_text('保留', encoding='utf-8')
            self.roots[label] = folder
        self.manager = ReleaseManager(self.root, self.data)
        self.old = self.release('2026.09.16.1')
        self.new = self.release('2026.09.16.2')
        atomic_json(self.manager.pointer, self.manager.pointer_for(self.old))

    def release(self, version):
        import os
        folder = self.root / 'releases' / version
        python = folder / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        python.parent.mkdir(parents=True)
        python.write_bytes(b'isolated-test-runtime')
        atomic_json(folder / 'manifest.json', manifest(version))
        atomic_json(folder / 'runtime-validated.json', {'fixture': True})
        return folder

    def migrate(self, pointer, database):
        with closing(sqlite3.connect(database)) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS migrations (version TEXT)')
                db.execute('INSERT INTO migrations VALUES (?)', (pointer['version'],))

    def reconcile(self, before, after):
        with closing(sqlite3.connect(before)) as old, closing(sqlite3.connect(after)) as new:
            self.assertEqual(old.execute('SELECT * FROM uploads').fetchall(), new.execute('SELECT * FROM uploads').fetchall())

    def install(self, **kwargs):
        args = dict(database=self.db, roots=self.roots, schema=1,
                    required_capabilities=['locked-history-v1'], quiesce=lambda: None,
                    migrate=self.migrate, reconcile=self.reconcile,
                    healthcheck=lambda pointer: None, verify=lambda: None)
        args.update(kwargs)
        return self.manager.install_release(self.new, **args)

    def test_new_business_writes_survive_compatible_code_rollback(self):
        self.install()
        with closing(sqlite3.connect(self.db)) as db:
            with db:
                db.execute("INSERT INTO uploads(name) VALUES ('升级后新上传')")
        self.manager.rollback_code(self.old, schema=2, required_capabilities=['locked-history-v1'],
                                   quiesce=lambda: None, healthcheck=lambda pointer: None)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0], 2)
        for label in self.roots:
            self.assertTrue((self.data / 'backups/2026.09.16.2' / label / 'keep.txt').exists())
        self.assertEqual(json.loads(self.manager.pointer.read_text())['version'], '2026.09.16.1')

    def test_incompatible_schema_and_missing_semantics_rejected(self):
        with self.assertRaises(ReleaseError):
            compatible(manifest('2026.09.16.2'), 3, [])
        value = manifest('2026.09.16.2')
        value['capabilities'].remove('locked-history-v1')
        with self.assertRaises(ReleaseError):
            validate_manifest(value)

    def test_failed_authentication_does_not_activate_or_touch_db(self):
        before = self.db.read_bytes()
        def fail():
            raise ReleaseError('invalid signature')
        with self.assertRaises(ReleaseError):
            self.install(verify=fail)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(json.loads(self.manager.pointer.read_text())['version'], '2026.09.16.1')

    def test_failed_trial_migration_keeps_original_data_and_pointer(self):
        before = self.db.read_bytes()
        def fail(pointer, database):
            raise OSError('disk full')
        with self.assertRaises(OSError):
            self.install(migrate=fail)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(json.loads(self.manager.journal.read_text())['phase'], 'RECOVERY_REQUIRED')
        with self.assertRaises(ReleaseError):
            self.install()

    def test_health_failure_recovers_compatible_prior_code_without_restoring_db(self):
        health_calls = []

        def health(pointer):
            health_calls.append(pointer['version'])
            if pointer['version'] == '2026.09.16.2':
                raise RuntimeError('candidate is not ready')

        with self.assertRaises(ReleaseError):
            self.install(schema_reader=lambda database: 2, healthcheck=health)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0], 1)
        self.assertEqual(health_calls, ['2026.09.16.2', '2026.09.16.1'])
        self.assertEqual(json.loads(self.manager.pointer.read_text())['version'], '2026.09.16.1')
        journal = json.loads(self.manager.journal.read_text())
        self.assertEqual(journal['phase'], 'ROLLBACK_COMPLETE')
        self.assertTrue(journal['recovered'])
        self.assertEqual(journal['prior']['version'], '2026.09.16.1')
        self.assertTrue((self.data / 'backups/2026.09.16.2/database.sqlite3').exists())

    def test_health_failure_keeps_maintenance_when_prior_code_is_incompatible(self):
        def health(pointer):
            raise RuntimeError('candidate is not ready')

        with self.assertRaises(RuntimeError):
            self.install(schema_reader=lambda database: 3, healthcheck=health)
        self.assertEqual(json.loads(self.manager.pointer.read_text())['version'], '2026.09.16.2')
        journal = json.loads(self.manager.journal.read_text())
        self.assertEqual(journal['phase'], 'RECOVERY_REQUIRED')
        self.assertEqual(journal['prior']['version'], '2026.09.16.1')
        self.assertTrue((self.data / 'backups/2026.09.16.2/database.sqlite3').exists())
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0], 1)

    def test_placeholder_commit_is_rejected(self):
        value = manifest('2026.09.16.2')
        value['commit_sha'] = '0' * 40
        with self.assertRaises(ReleaseError):
            validate_manifest(value)
