from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from scripts.release_adapter import adapters, reconcile_database
from scripts.release_manager import ReleaseError, verify_attestation, compatible, upgrade_compatible
from tests.test_release_manager import manifest


class HealthResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size):
        return self.payload


class ReleaseAdapterTests(unittest.TestCase):
    def test_business_reconciliation_rejects_changed_financial_value(self):
        with tempfile.TemporaryDirectory() as directory:
            before, after = (Path(directory) / name for name in ('before.sqlite', 'after.sqlite'))
            for path in (before, after):
                with closing(sqlite3.connect(path)) as db:
                    with db:
                        db.execute('CREATE TABLE budgeting_value(id INTEGER PRIMARY KEY, cents INTEGER)')
                        db.execute('INSERT INTO budgeting_value VALUES (1, 12345)')
            reconcile_database(before, after)
            with closing(sqlite3.connect(after)) as db:
                with db:
                    db.execute('UPDATE budgeting_value SET cents=12346')
            with self.assertRaises(ReleaseError):
                reconcile_database(before, after)

    def test_authentication_binds_repository_workflow_and_exact_commit(self):
        release = manifest('2026.09.16.2')
        with mock.patch('scripts.release_manager.shutil.which', return_value='/bin/gh'), mock.patch('scripts.release_manager.subprocess.run') as run:
            run.return_value.returncode = 0
            verify_attestation(Path('/tmp/package.zip'), release, token='private-token')
            args, kwargs = run.call_args
            self.assertIn('--source-digest', args[0])
            self.assertIn(release['commit_sha'], args[0])
            self.assertIn('--deny-self-hosted-runners', args[0])
            self.assertNotIn('private-token', args[0])
            self.assertEqual(kwargs['env']['GH_TOKEN'], 'private-token')
            run.return_value.returncode = 1
            run.return_value.stderr = b'403 forbidden private-token'
            with self.assertRaisesRegex(ReleaseError, 'Contents: read.*Attestations: read') as failure:
                verify_attestation(Path('/tmp/package.zip'), release)
            self.assertNotIn('private-token', str(failure.exception))

    def test_migration_entry_is_not_a_code_rollback_compatibility_claim(self):
        release = manifest('2026.09.16.2')
        release.update(database_schema_read_range=[14, 14], database_schema_write_range=[14, 14], upgrade_from_schema_range=[11, 14])
        upgrade_compatible(release, 11, ['locked-history-v1'])
        with self.assertRaises(ReleaseError):
            compatible(release, 11, ['locked-history-v1'])
        compatible(release, 14, ['locked-history-v1'])

    def test_local_registration_cannot_authenticate_remote_package(self):
        release = manifest('2026.09.16.2')
        release.update(authenticity='local-admin-registration', local_registration=True)
        with self.assertRaises(ReleaseError):
            verify_attestation(Path('/tmp/package.zip'), release)

    def test_healthcheck_waits_for_worker_heartbeat(self):
        runtime = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(runtime, ignore_errors=True))
        update = SimpleNamespace(
            read_json=lambda path: {'port': 18768},
            runtime=lambda name: runtime / name,
        )
        response = HealthResponse(b'{"database": true, "storage": true, "worker_healthy": false}')
        ready = HealthResponse(b'{"database": true, "storage": true, "worker_healthy": true}')
        with mock.patch('scripts.release_adapter.execute') as execute, \
             mock.patch('scripts.release_adapter.urllib.request.urlopen', side_effect=[response, ready]), \
             mock.patch('scripts.release_adapter.time.sleep'):
            _, _, healthcheck = adapters(update, {})
            healthcheck({'python': 'python', 'release_dir': '/tmp/release'})
        self.assertEqual(execute.call_count, 1)

    def test_healthcheck_rejects_missing_worker_heartbeat(self):
        runtime = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(runtime, ignore_errors=True))
        update = SimpleNamespace(
            read_json=lambda path: {'port': 18768},
            runtime=lambda name: runtime / name,
        )
        response = HealthResponse(b'{"database": true, "storage": true, "worker_healthy": false}')
        with mock.patch('scripts.release_adapter.execute'), \
             mock.patch('scripts.release_adapter.urllib.request.urlopen', return_value=response), \
             mock.patch('scripts.release_adapter.time.monotonic', side_effect=[0, 31]), \
             self.assertRaisesRegex(ReleaseError, 'worker_healthy'):
            _, _, healthcheck = adapters(update, {})
            healthcheck({'python': 'python', 'release_dir': '/tmp/release'})
