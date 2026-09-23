"""Checksum rejection protects bundled installers before Windows executes them."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.prepare_windows_offline import verified


class OfflineIntegrityTests(unittest.TestCase):
    def test_missing_and_tampered_installer_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = Path(directory) / 'installer.exe'
            digest = hashlib.sha256(b'official installer').hexdigest()
            self.assertFalse(verified(installer, digest))
            installer.write_bytes(b'official installer')
            self.assertTrue(verified(installer, digest))
            installer.write_bytes(b'changed installer')
            self.assertFalse(verified(installer, digest))
