import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from scripts import github_cli


class GitHubCliTests(unittest.TestCase):
    def archive(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as package:
            package.writestr('bin/gh.exe', b'test verifier')
        return data.getvalue()

    def test_windows_download_verifies_digest_and_reuses_valid_binary(self):
        payload = self.archive()
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as root, patch.object(github_cli.platform, 'system', return_value='Windows'), patch.object(github_cli.platform, 'machine', return_value='AMD64'), patch.dict(github_cli.ASSETS, {('Windows', 'amd64'): ('windows_amd64', digest)}), patch.object(github_cli.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as download:
            result = Path(github_cli.ensure_github_cli(root))
            self.assertEqual(result.read_bytes(), b'test verifier')
            self.assertEqual(github_cli.ensure_github_cli(root), str(result))
            self.assertEqual(download.call_count, 1)
            self.assertTrue(download.call_args.args[0].startswith('https://github.com/cli/cli/releases/download/'))

    def test_corrupt_download_cannot_install_executable(self):
        with tempfile.TemporaryDirectory() as root, patch.object(github_cli.platform, 'system', return_value='Windows'), patch.object(github_cli.platform, 'machine', return_value='AMD64'), patch.object(github_cli.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupt')):
            with self.assertRaisesRegex(ValueError, '校验失败'):
                github_cli.ensure_github_cli(root)
            self.assertFalse(list(Path(root).rglob('gh.exe')))
