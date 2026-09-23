import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import TestCase, mock
from budgeting.excel.recalc import discover_soffice, recalc_with_libreoffice, RecalcInfrastructureError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import local_server


class RuntimeRefactorTests(TestCase):
    def test_explicit_missing_office_does_not_silently_fallback(self):
        with self.assertRaises(RecalcInfrastructureError):
            discover_soffice('/does-not-exist/soffice')

    def test_recalc_profile_is_uri_and_timeout_stops_own_tree(self):
        import subprocess
        with tempfile.TemporaryDirectory(prefix='预算 test ') as folder:
            source = Path(folder) / '底稿.xlsx'; source.write_bytes(b'original')
            process = mock.Mock(pid=321)
            process.communicate.side_effect = [subprocess.TimeoutExpired('soffice', 1), ('', '')]
            with (mock.patch('budgeting.excel.recalc.discover_soffice', return_value=Path('/office')),
                 mock.patch('budgeting.excel.recalc.subprocess.Popen', return_value=process) as popen,
                 mock.patch('scripts.runtime_support.stop_process_tree') as stop):
                with self.assertRaises(RecalcInfrastructureError):
                    recalc_with_libreoffice(source, timeout=1)
                argument = next(a for a in popen.call_args.args[0] if a.startswith('-env:'))
                self.assertTrue(argument.startswith('-env:UserInstallation=file:///'))
                self.assertNotIn(' ', argument)
                stop.assert_called_once_with(321, timeout=5)
                self.assertEqual(source.read_bytes(), b'original')

    def test_bad_release_pointer_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'active-release.json').write_text(json.dumps({'schema': 1, 'release_dir': '/tmp/unowned', 'python': sys.executable}))
            with mock.patch.dict(os.environ, {'BUDGET_INSTALL_ROOT': str(root)}):
                with self.assertRaises(RuntimeError):
                    local_server.dispatch_active_release()

    def test_daily_start_rejects_pending_schema_without_starting_children(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            with (mock.patch.object(local_server, "RUNTIME", Path(folder)),
                  mock.patch.object(local_server, "LOGS", Path(folder)),
                  mock.patch.object(local_server.subprocess, "run", side_effect=[None, subprocess.CalledProcessError(1, "schema-check")]) as run,
                  mock.patch.object(local_server.subprocess, "Popen") as popen):
                with self.assertRaises(subprocess.CalledProcessError):
                    local_server.serve(18769)
                self.assertEqual(run.call_args.args[0][-3:], ["migrate", "--check", "--noinput"])
                popen.assert_not_called()

    def test_shared_heartbeat_checks_age_and_process_identity(self):
        from budgeting.services.worker_heartbeat import read_worker_status
        import psutil
        import time
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "worker-heartbeat.json"
            data = {"pid": os.getpid(), "created": psutil.Process().create_time(),
                    "heartbeat_at": time.time(), "state": "IDLE"}
            path.write_text(json.dumps(data))
            self.assertTrue(read_worker_status(folder)["healthy"])
            self.assertNotIn("pid", read_worker_status(folder))
            data["created"] -= 1; path.write_text(json.dumps(data))
            self.assertFalse(read_worker_status(folder)["healthy"])
            data["created"] += 1; data["heartbeat_at"] -= 30; path.write_text(json.dumps(data))
            self.assertFalse(read_worker_status(folder)["healthy"])
