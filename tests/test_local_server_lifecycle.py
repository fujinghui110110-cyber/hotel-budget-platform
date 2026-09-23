import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from unittest import TestCase, mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location('local_server_lifecycle', SCRIPT_DIR / 'local_server.py')
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class ServerLifecycleTests(TestCase):
    def test_dead_supervisor_still_cleans_verified_children(self):
        identities = [{'pid': 98765, 'create_time': 1, 'cmdline': ['worker'], 'cwd': '/tmp'}]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'server.json'
            state.write_text(json.dumps({'pid': 1234, 'children_identity': identities}))
            with mock.patch.object(server, 'STATE', state), mock.patch.object(server, 'owned', return_value=False), mock.patch.object(server.sys, 'platform', 'win32'), mock.patch.object(server, 'stop_identities') as stop:
                server.stop()
                stop.assert_called_once_with(identities)
                self.assertFalse(state.exists())

    def test_state_refresh_captures_restarted_child_pid(self):
        children = [mock.Mock(pid=2), mock.Mock(pid=3)]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(server, 'STATE', Path(directory) / 'server.json'), mock.patch.object(server, 'process_identity', side_effect=lambda pid: {'pid': pid}):
            server.write_server_state(children, 8768)
            children[1] = mock.Mock(pid=4)
            server.write_server_state(children, 8768)
            result = json.loads(server.STATE.read_text())
            self.assertEqual(result['worker_pid'], 4)
            self.assertEqual(result['children_identity'], [{'pid': 2}, {'pid': 4}])

    def test_legacy_record_does_not_kill_unrelated_process(self):
        with mock.patch.object(server, 'process_identity', return_value={'pid': 999, 'cwd': '/another-project', 'cmdline': ['python', 'budget_worker']}):
            with self.assertRaises(RuntimeError):
                server.legacy_children({'worker_pid': 999})
