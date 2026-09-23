from pathlib import Path
import tempfile
from unittest import mock

from django.contrib.auth import authenticate, get_user_model
from django.test import TestCase, override_settings
from scripts.admin_access import provision


class AdminAccessTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def password(self, result):
        return next(line for line in result['path'].read_text(encoding='utf-8-sig').splitlines() if line.startswith('密码：')).split('：', 1)[1]

    def test_fresh_install_has_working_strong_admin_without_input(self):
        result = provision(self.root)
        user = authenticate(username=result['username'], password=self.password(result))
        self.assertIsNotNone(user)
        self.assertTrue(user.is_admin_role)
        with override_settings(PUBLIC_ACCESS=True):
            self.assertIsNotNone(authenticate(username=user.username, password=self.password(result)))
        self.assertNotIn('admin123', self.password(result))

    def test_reinstall_preserves_existing_account_and_password(self):
        first = provision(self.root)
        self.assertIsNone(provision(self.root))
        self.assertIsNotNone(authenticate(username=first['username'], password=self.password(first)))
        self.assertEqual(len(list((self.root/'本机账号信息').glob('*.txt'))), 1)

    def test_repair_changes_only_selected_admin(self):
        first = provision(self.root)
        User = get_user_model()
        other = User.objects.create_user(username='other', password='Other-Secret-123!', role='ADMIN')
        other_hash = other.password
        result = provision(self.root, reset_username=first['username'])
        self.assertIsNone(authenticate(username=first['username'], password=self.password(first)))
        self.assertIsNotNone(authenticate(username=result['username'], password=self.password(result)))
        other.refresh_from_db()
        self.assertEqual(other.password, other_hash)

    def test_project_named_admin_is_not_promoted(self):
        user = get_user_model().objects.create_user(username='admin', password='Project-Secret-123!', role='PROJECT')
        result = provision(self.root)
        self.assertEqual(result['username'], 'admin2')
        user.refresh_from_db()
        self.assertFalse(user.is_admin_role)
        with self.assertRaises(ValueError):
            provision(self.root, reset_username='admin')

    def test_file_failure_rolls_back_account_password_change(self):
        first = provision(self.root)
        with mock.patch('scripts.admin_access.write_credentials', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                provision(self.root, reset_username=first['username'])
        self.assertIsNotNone(authenticate(username=first['username'], password=self.password(first)))
