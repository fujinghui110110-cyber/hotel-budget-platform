import importlib.util
from pathlib import Path
import sys
from unittest import TestCase, mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location('stop_all', SCRIPT_DIR / 'stop_all.py')
stop_all = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stop_all)


class StopAllTests(TestCase):
    def test_waits_for_restart_then_requests_stop_before_local_stop(self):
        states = [
            {'busy': True, 'status': 'restarting'},
            {'busy': False, 'status': 'started', 'running': True},
            {'busy': True, 'status': 'stopping'},
            {'busy': False, 'status': 'stopped', 'running': False},
        ]
        with mock.patch.object(stop_all.public_access, 'status', side_effect=states), mock.patch.object(stop_all.public_access, 'request_action', return_value={'status': 'stopping'}) as request, mock.patch.object(stop_all, 'public_port_open', return_value=False), mock.patch.object(stop_all.local_server, 'stop') as local, mock.patch.object(stop_all.time, 'sleep'):
            stop_all.stop_all()
            request.assert_called_once_with('stop')
            local.assert_called_once()

    def test_racing_restart_is_not_mistaken_for_stop(self):
        with mock.patch.object(stop_all.public_access, 'status', side_effect=[{'status': 'started'}, {'status': 'started'}, {'status': 'stopped'}]), mock.patch.object(stop_all.public_access, 'request_action', side_effect=[{'status': 'restarting'}, {'status': 'stopped'}]) as request, mock.patch.object(stop_all, 'public_port_open', return_value=False), mock.patch.object(stop_all.local_server, 'stop') as local, mock.patch.object(stop_all.time, 'sleep'):
            stop_all.stop_all()
            self.assertEqual(request.call_count, 2)
            local.assert_called_once()

    def test_open_public_port_preserves_local_service(self):
        with mock.patch.object(stop_all.public_access, 'status', side_effect=[{'status': 'started'}, {'status': 'stopped'}]), mock.patch.object(stop_all.public_access, 'request_action', return_value={'status': 'stopping'}), mock.patch.object(stop_all, 'public_port_open', return_value=True), mock.patch.object(stop_all.local_server, 'stop') as local, mock.patch.object(stop_all.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '端口仍被占用'):
                stop_all.stop_all()
            local.assert_not_called()

    def test_timeout_preserves_local_service(self):
        with mock.patch.object(stop_all.time, 'monotonic', side_effect=[0, 301]), mock.patch.object(stop_all.local_server, 'stop') as local:
            with self.assertRaisesRegex(RuntimeError, '超过 300 秒'):
                stop_all.stop_all()
            local.assert_not_called()
