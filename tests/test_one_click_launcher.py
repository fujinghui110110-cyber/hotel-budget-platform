import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == 'nt', 'POSIX launcher requires sh; Windows acceptance runs on Windows')
class OneClickLauncherTests(unittest.TestCase):
    def invoke(self, name='一键启动.command', code=0):
        with tempfile.TemporaryDirectory(prefix='budget launcher ') as folder:
            root = Path(folder)
            relative = Path(name)
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
            python = root / '.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/sh\npwd\nprintf "%s\\n" "$@"\nexit ' + str(code) + '\n')
            python.chmod(0o755)
            env = dict(os.environ)
            env.pop('PYTHON_BIN', None)
            result = subprocess.run(['sh', str(target)], cwd='/', env=env,
                                    text=True, capture_output=True)
            return result, str(root)

    def test_double_click_from_another_directory_opens_browser_after_start(self):
        result, root = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(root + '\n' + root + '/scripts/local_server.py\nstart\n--open', result.stdout)

    def test_start_failure_is_not_reported_as_success(self):
        result, _ = self.invoke(code=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('启动未完成', result.stdout)

    def test_relocated_stop_tool_resolves_installation_root(self):
        result, root = self.invoke('scripts/maintenance/停止预算统筹系统.command')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(root + '\nscripts/stop_all.py', result.stdout)

    def test_relocated_autostart_calls_service_not_start_launcher(self):
        result, root = self.invoke('scripts/maintenance/启用预算系统登录后自启.command')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(root + '/scripts/local_server.py\nenable-autostart', result.stdout)
        self.assertNotIn('--open', result.stdout)
