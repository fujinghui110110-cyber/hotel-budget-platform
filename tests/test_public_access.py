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
    def test_only_success_banner_can_supply_public_url(self):
        error = 'failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": context deadline exceeded'
        self.assertIsNone(public_access.generated_tunnel_url(error))
        banner = '2026-09-11 INF | https://actual-budget-link.trycloudflare.com |'
        self.assertEqual(public_access.generated_tunnel_url(error + '\n' + banner), 'https://actual-budget-link.trycloudflare.com')

    def test_api_timeout_does_not_start_web_or_report_web_failure(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            awake = mock.Mock(pid=100)
            awake.poll.return_value = None
            tunnel = mock.Mock(pid=101)
            tunnel.poll.return_value = 1

            def spawn(command, **kwargs):
                if command[0] == '/usr/bin/caffeinate':
                    return awake
                kwargs['stdout'].write('Post "https://api.trycloudflare.com/tunnel": context deadline exceeded\n')
                kwargs['stdout'].flush()
                return tunnel

            with mock.patch.multiple(public_access, RUNTIME=root, STATE=root/'state.json', STOP_REQUEST=root/'stop', LOGS=root), \
                 mock.patch.object(public_access, 'security_check'), mock.patch.object(public_access, 'ensure_binary', return_value=root/'cloudflared'), \
                 mock.patch.object(public_access, 'process_identity', return_value=None), mock.patch.object(public_access.sys, 'platform', 'darwin'), \
                 mock.patch.object(public_access.signal, 'signal'), mock.patch.object(public_access.subprocess, 'Popen', side_effect=spawn) as popen:
                with self.assertRaises(SystemExit):
                    public_access.serve()
            state = json.loads((root/'state.json').read_text())
            self.assertIn('超时', state['error'])
            self.assertNotIn('网页服务', state['error'])
            self.assertEqual(popen.call_count, 2)
            self.assertIsNone(state['url'])

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
                if os.name == 'nt':
                    import subprocess
                    acl = subprocess.check_output(['icacls', str(root / 'public-secret-key')])
                    self.assertNotIn(b'(I)', acl, 'Secret must not inherit directory access')
                else:
                    self.assertEqual(os.stat(root / 'public-secret-key').st_mode & 0o777, 0o600)
                self.assertEqual(public_access.public_environment('https://other.trycloudflare.com')['DJANGO_SECRET_KEY'], env['DJANGO_SECRET_KEY'])

    def test_active_release_reads_original_install_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory)
            (install / '.env').write_text('DATABASE_PATH=' + str(install / 'existing.sqlite3') + '\n')
            environment = {k: v for k, v in os.environ.items() if k != 'DATABASE_PATH'}
            environment['BUDGET_INSTALL_ROOT'] = str(install)
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(public_access, 'RUNTIME', install), mock.patch.object(public_access, 'ROOT', install / 'releases' / 'new'):
                env = public_access.public_environment('https://test-budget.trycloudflare.com')
            self.assertEqual(env['DATABASE_PATH'], str(install / 'existing.sqlite3'))

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
                 mock.patch.object(public_access, 'STOP_REQUEST', root / 'stop'), \
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

class PublicControllerTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        for name, value in {'RUNTIME': root, 'STATE': root / 'state.json',
                            'OPERATION': root / 'operation.json', 'STOP_REQUEST': root / 'stop',
                            'LOGS': root}.items():
            patch = mock.patch.object(public_access, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_restart_is_nonblocking_and_duplicate_is_not_spawned(self):
        child = mock.Mock(pid=999)
        with mock.patch.object(public_access.subprocess, 'Popen', return_value=child) as spawn, \
             mock.patch.object(public_access, 'process_matches', return_value=True):
            first = public_access.request_action('restart')
            second = public_access.request_action('restart')
        self.assertEqual(first['status'], 'restarting')
        self.assertTrue(first['busy'])
        self.assertTrue(second['busy'])
        self.assertEqual(spawn.call_count, 1)
        child.wait.assert_not_called()
        self.assertIn('_control', spawn.call_args.args[0])

    def test_spawn_failure_is_visible_and_retryable(self):
        with mock.patch.object(public_access.subprocess, 'Popen', side_effect=OSError('permission denied')):
            result = public_access.request_action('restart')
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['busy'])
        self.assertIn('permission denied', result['error'])

    def test_dead_controller_is_not_stuck_busy(self):
        public_access._write(public_access.OPERATION, {'status': 'restarting', 'pid': 999})
        with mock.patch.object(public_access, 'process_matches', return_value=False):
            result = public_access.status()
        self.assertFalse(result['busy'])
        self.assertEqual(result['status'], 'failed')

    def test_stop_does_not_kill_an_unrelated_pid(self):
        public_access._write(public_access.STATE, {'status': 'started', 'pid': 999})
        with mock.patch.object(public_access, 'supervisor_alive', return_value=False), \
             mock.patch.object(public_access, 'stop_process_tree') as kill, \
             mock.patch.object(public_access.os, 'kill') as signal:
            public_access._stop()
        kill.assert_not_called()
        signal.assert_not_called()
        self.assertEqual(public_access.status()['status'], 'stopped')

    def test_action_allowlist(self):
        with self.assertRaises(ValueError):
            public_access.request_action('restart; echo injected')

    def test_controller_reports_security_failure(self):
        public_access._write(public_access.OPERATION, {'token': 'test', 'status': 'restarting'})
        with mock.patch.object(public_access, '_stop'), \
             mock.patch.object(public_access, 'public_environment', return_value={}), \
             mock.patch.object(public_access, 'security_check', side_effect=RuntimeError('weak password')), \
             mock.patch.object(public_access.subprocess, 'Popen') as spawn:
            public_access.control('restart', 'test')
        spawn.assert_not_called()
        self.assertEqual(public_access.status()['error'], 'weak password')

class PublicBinaryInstallTests(TestCase):
    def archive(self, name='cloudflared', symlink=False):
        import io
        import tarfile
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode='w:gz') as archive:
            item = tarfile.TarInfo(name)
            if symlink:
                item.type = tarfile.SYMTYPE
                item.linkname = '/tmp/elsewhere'
                archive.addfile(item)
            else:
                item.size = 4
                archive.addfile(item, io.BytesIO(b'test'))
        return content.getvalue()

    def install(self, root, archive, machine='arm64', digest=None):
        import hashlib
        import io
        import json
        asset_name = 'cloudflared-darwin-' + ('arm64' if machine == 'arm64' else 'amd64') + '.tgz'
        release = {'assets': [{'name': asset_name, 'digest': digest or 'sha256:' + hashlib.sha256(archive).hexdigest(),
                              'browser_download_url': 'https://github.com/cloudflare/cloudflared/releases/download/test/' + asset_name}]}
        with mock.patch.object(public_access, 'RUNTIME', root), \
             mock.patch.object(public_access.sys, 'platform', 'darwin'), \
             mock.patch('platform.machine', return_value=machine), \
             mock.patch('urllib.request.urlopen', side_effect=[io.BytesIO(json.dumps(release).encode()), io.BytesIO(archive)]):
            return public_access.ensure_binary()

    def test_mac_arm_and_intel_verified_archive_installs_executable(self):
        for machine in ('arm64', 'x86_64'):
            with self.subTest(machine=machine), tempfile.TemporaryDirectory() as directory:
                binary = self.install(Path(directory), self.archive(), machine)
                self.assertEqual(binary.read_bytes(), b'test')
                if os.name != 'nt':
                    self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
                else:
                    self.assertTrue(os.access(binary, os.R_OK))

    def test_archive_traversal_link_and_wrong_hash_rejected(self):
        for payload, digest in [(self.archive('../cloudflared'), None),
                                (self.archive('/cloudflared'), None),
                                (self.archive(symlink=True), None),
                                (self.archive(), 'sha256:' + '0' * 64)]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(RuntimeError):
                    self.install(root, payload, digest=digest)
                self.assertFalse((root / 'bin' / 'cloudflared').exists())
                self.assertFalse(list((root / 'bin').glob('*.download')))
                self.assertFalse(list((root / 'bin').glob('*.unpacked')))

class PublicOrphanCleanupTests(TestCase):
    setUp = PublicControllerTests.setUp

    def test_dead_parent_still_cleans_recorded_children(self):
        identities = [{'pid': 123, 'create_time': 10, 'cmdline': ['verified'], 'cwd': '/budget'}]
        public_access._write(public_access.STATE, {'pid': 999, 'children': [123],
                                                 'children_identity': identities})
        with mock.patch.object(public_access, 'supervisor_alive', return_value=False), \
             mock.patch.object(public_access, 'stop_identities') as cleanup:
            public_access._stop()
        cleanup.assert_called_once_with(identities, timeout=10)
        self.assertEqual(public_access.status()['status'], 'stopped')

    def test_legacy_only_adopts_exact_project_command_and_directory(self):
        command = public_access.wsgi_command(public_access.PORT, public=True)
        valid = {'pid': 1, 'create_time': 10, 'cmdline': command, 'cwd': str(public_access.ROOT)}
        other_directory = {**valid, 'pid': 2, 'cwd': '/another/project'}
        altered_command = {**valid, 'pid': 3, 'cmdline': command + ['unexpected']}
        with mock.patch.object(public_access, 'process_identity', side_effect=[valid, other_directory, altered_command]):
            result = public_access._legacy_children({'pid': 999, 'children': [1, 2, 3]})
        self.assertEqual(result, [valid])

    def test_cleanup_failure_does_not_claim_stopped(self):
        public_access._write(public_access.STATE, {'status': 'started', 'pid': 999, 'children_identity': []})
        with mock.patch.object(public_access, 'supervisor_alive', return_value=False), \
             mock.patch.object(public_access, 'stop_identities', side_effect=RuntimeError('still alive')):
            with self.assertRaises(RuntimeError):
                public_access._stop()
        self.assertEqual(public_access._read(public_access.STATE)['status'], 'started')
