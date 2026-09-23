"""Runtime boundary checks; Windows system calls use stubs on non-Windows hosts."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

SPEC = importlib.util.spec_from_file_location('runtime_support', Path(__file__).resolve().parents[1] / 'scripts/runtime_support.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeSupportTests(TestCase):
    def test_lock_rejects_concurrent_holder_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.lock'
            with runtime.FileLock(path):
                with self.assertRaises(BlockingIOError):
                    with runtime.FileLock(path):
                        pass
            with runtime.FileLock(path):
                pass

    def test_windows_lock_uses_fixed_byte_and_unlocks(self):
        fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=mock.Mock())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(runtime, 'HOST_WINDOWS', True), mock.patch.dict(sys.modules, {'msvcrt': fake}):
            path = Path(directory) / 'test.lock'
            with runtime.FileLock(path):
                self.assertEqual(path.read_bytes(), b'0')
            self.assertEqual([call.args[1:] for call in fake.locking.call_args_list], [(2, 1), (0, 1)])

    def test_windows_uses_waitress_and_detached_process(self):
        with mock.patch.object(runtime.sys, 'platform', 'win32'), mock.patch.object(subprocess, 'CREATE_NEW_PROCESS_GROUP', 512, create=True), mock.patch.object(subprocess, 'DETACHED_PROCESS', 8, create=True):
            command = runtime.wsgi_command(8768)
            self.assertIn('waitress', command)
            self.assertIn('--listen=127.0.0.1:8768', command)
            self.assertEqual(runtime.detached_popen_kwargs(), {'creationflags': 520})

    def test_process_matching_is_exact_not_substring(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/local_server.py'
        process = mock.Mock()
        process.cmdline.return_value = [sys.executable, str(script) + '.other', 'serve']
        with mock.patch.object(runtime, 'pid_alive', return_value=True), mock.patch.object(runtime.psutil, 'Process', return_value=process):
            self.assertFalse(runtime.process_matches(123, script, 'serve'))
            process.cmdline.return_value = [sys.executable, str(script), 'serve']
            self.assertTrue(runtime.process_matches(123, script, 'serve'))
            self.assertFalse(runtime.process_matches(123, script, '_serve'))

    def test_identity_rejects_reused_pid(self):
        saved = {'pid': 999, 'create_time': 100, 'cmdline': ['python'], 'cwd': '/tmp'}
        with mock.patch.object(runtime, 'process_identity', return_value={**saved, 'create_time': 101}), mock.patch.object(runtime, 'stop_process_tree') as stop:
            runtime.stop_identities([saved])
            stop.assert_not_called()

    def test_force_killed_children_are_waited_with_parent(self):
        parent, child = mock.Mock(), mock.Mock()
        parent.children.return_value = [child]
        with mock.patch.object(runtime.psutil, 'Process', return_value=parent), mock.patch.object(runtime.psutil, 'wait_procs', side_effect=[([], [child]), ([child, parent], [])]) as wait:
            runtime.stop_process_tree(999)
            child.kill.assert_called_once()
            self.assertEqual(wait.call_args_list[1].args[0], [child, parent])

    def test_surviving_child_reports_failure_and_resumes_parent(self):
        parent, child = mock.Mock(), mock.Mock()
        parent.children.return_value = [child]
        with mock.patch.object(runtime.psutil, 'Process', return_value=parent), mock.patch.object(runtime.psutil, 'wait_procs', side_effect=[([], [child]), ([], [child])]):
            with self.assertRaises(RuntimeError):
                runtime.stop_process_tree(999)
            parent.resume.assert_called_once()
            parent.kill.assert_called_once()
