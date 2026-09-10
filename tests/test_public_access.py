"""Public origin configuration must remain isolated and narrowly scoped."""
import importlib.util
import os
from pathlib import Path
import tempfile
from unittest import TestCase, mock

SPEC = importlib.util.spec_from_file_location('public_access', Path(__file__).resolve().parents[1] / 'scripts/public_access.py')
public_access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(public_access)


class PublicAccessTests(TestCase):
    def test_exact_https_origin_and_private_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(public_access, 'RUNTIME', root), mock.patch.object(public_access, 'ROOT', root):
                env = public_access.public_environment('https://test-budget.trycloudflare.com')
                self.assertEqual(env['DJANGO_ALLOWED_HOSTS'], 'test-budget.trycloudflare.com')
                self.assertEqual(env['CSRF_TRUSTED_ORIGINS'], 'https://test-budget.trycloudflare.com')
                for name in ('PUBLIC_ACCESS', 'TRUST_PROXY', 'SESSION_COOKIE_SECURE', 'CSRF_COOKIE_SECURE'):
                    self.assertEqual(env[name], '1')
                self.assertEqual(env['DJANGO_DEBUG'], '0')
                self.assertGreaterEqual(len(env['DJANGO_SECRET_KEY']), 50)
                self.assertEqual(os.stat(root / 'public-secret-key').st_mode & 0o777, 0o600)
                self.assertEqual(public_access.public_environment('https://other.trycloudflare.com')['DJANGO_SECRET_KEY'], env['DJANGO_SECRET_KEY'])

    def test_reject_arbitrary_origin_and_wildcards(self):
        for url in ('http://test.trycloudflare.com', 'https://*.trycloudflare.com', 'https://evil.com',
                    'https://good.trycloudflare.com.evil.com', 'https://good.trycloudflare.com/path'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_access.public_environment(url)

    def test_stale_pid_cannot_stop_unrelated_process(self):
        with mock.patch.object(public_access, 'alive', return_value=True), mock.patch.object(public_access.subprocess, 'run') as run:
            run.return_value.stdout = '/usr/bin/unrelated-server'
            self.assertFalse(public_access.supervisor_alive(123))

    def test_failed_tunnel_releases_sleep_assertion_and_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            (root / 'bin' / 'cloudflared').touch()
            awake = mock.Mock(pid=100)
            awake.poll.return_value = None
            tunnel = mock.Mock(pid=101)
            tunnel.poll.return_value = 1
            with mock.patch.object(public_access, 'RUNTIME', root), \
                 mock.patch.object(public_access, 'STATE', root / 'state.json'), \
                 mock.patch.object(public_access, 'LOGS', root), \
                 mock.patch.object(public_access, 'public_environment', return_value={}), \
                 mock.patch.object(public_access, 'security_check'), \
                 mock.patch.object(public_access.sys, 'platform', 'darwin'), \
                 mock.patch.object(public_access.signal, 'signal'), \
                 mock.patch.object(public_access.subprocess, 'Popen', side_effect=[awake, tunnel]) as popen:
                with self.assertRaises(SystemExit) as error:
                    public_access.serve()
                self.assertEqual(error.exception.code, 1)
                self.assertEqual(popen.call_args_list[0].args[0], ['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())])
                awake.terminate.assert_called_once()
                awake.wait.assert_called_once()
                import json
                state = json.loads((root / 'state.json').read_text())
                self.assertEqual(state['status'], 'failed')
                self.assertFalse(state['running'])
                self.assertEqual(state['children'], [])
