"""Fault injection at download/storage boundaries, using a real isolated installation."""
from contextlib import nullcontext
import errno
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.error import URLError

from scripts.release_adapter import apply_update
from scripts.release_manager import ReleaseError


class UpdateFailureBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='升级 故障验收 ')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.runtime = root / 'runtime'
        self.runtime.mkdir()
        data = root / 'data'
        data.mkdir()
        self.database = data / 'db.sqlite3'
        self.database.write_bytes(b'unchanged-business-data')
        self.pointer = root / 'active-release.json'
        self.pointer.write_text(json.dumps({'version': 'current'}))
        self.storage = data / 'storage'
        self.storage.mkdir()
        (self.storage / 'original.xlsx').write_bytes(b'preserved-original')
        self.states = []
        self.payload = b'partial-update'
        self.release = {'asset_id': 1, 'version': 'new', 'size': len(self.payload),
                        'sha256': hashlib.sha256(self.payload).hexdigest()}
        self.update = SimpleNamespace(ROOT=root, API='https://example.invalid', MAX_ARCHIVE=1024,
            UpdateError=ReleaseError, runtime=lambda name: self.runtime / name,
            FileLock=lambda path: nullcontext(), read_json=lambda path: self.release,
            state=lambda state, **fields: self.states.append((state, fields)),
            github_open=mock.Mock(return_value=io.BytesIO(self.payload)))
        self.env = {'DATABASE_PATH': str(self.database), 'BUDGET_INSTALL_ROOT': str(root),
                    'BUDGET_STORAGE_ROOT': str(self.storage)}

    def run_failure(self):
        with mock.patch('scripts.release_adapter.environment', return_value=self.env), \
             mock.patch('scripts.release_adapter.execute') as execute:
            apply_update(self.update)
        execute.assert_not_called()
        self.assertEqual(self.database.read_bytes(), b'unchanged-business-data')
        self.assertEqual(json.loads(self.pointer.read_text()), {'version': 'current'})
        self.assertEqual((self.storage / 'original.xlsx').read_bytes(), b'preserved-original')
        self.assertEqual(self.states[-1][0], 'failed')
        self.assertFalse(self.states[-1][1]['busy'])
        self.assertTrue(self.states[-1][1]['error'])
        self.assertNotIn('completed', [state for state, _ in self.states])
        self.assertFalse((self.runtime / 'update-maintenance').exists())
        self.assertFalse((Path(self.env['BUDGET_INSTALL_ROOT']) / 'releases' / 'new').exists())

    def test_insufficient_space_never_downloads_or_stops_service(self):
        with mock.patch('scripts.release_adapter.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            self.run_failure()
        self.update.github_open.assert_not_called()

    def test_connection_break_after_partial_download_never_activates(self):
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.read.side_effect = [self.payload[:4], URLError('connection interrupted')]
        self.update.github_open.return_value = stream
        self.run_failure()
        files = list((self.runtime / 'updates').glob('*/update.zip'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), self.payload[:4])

    def test_disk_fills_after_preflight_never_activates(self):
        original_open = Path.open
        def injected_open(path, *args, **kwargs):
            if path.name == 'update.zip' and args and args[0] == 'wb':
                context = mock.MagicMock()
                context.__enter__.return_value.write.side_effect = OSError(errno.ENOSPC, 'No space left')
                return context
            return original_open(path, *args, **kwargs)
        with mock.patch.object(Path, 'open', injected_open):
            self.run_failure()

    def test_wrong_download_hash_never_unpacks_or_activates(self):
        self.release['sha256'] = '0' * 64
        self.run_failure()
        self.assertIn('校验失败', self.states[-1][1]['error'])
