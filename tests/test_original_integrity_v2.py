import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from budgeting.models import BudgetCycle, Project, UploadVersion, ValidationIssue
from budgeting.services.workflow import process_upload


class OriginalIntegrityTests(TestCase):
    def test_tampered_original_is_rejected_before_recalculation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'original.xlsx').write_bytes(b'replaced-content')
            upload = UploadVersion.objects.create(
                project=Project.objects.create(code='INTEGRITY', name='原件完整性'),
                cycle=BudgetCycle.objects.create(name='2027', budget_year=2027),
                original_path='original.xlsx', sha256='0' * 64,
            )
            with override_settings(BUDGET_STORAGE_ROOT=root), patch('budgeting.services.workflow.recalc_with_libreoffice') as recalc:
                self.assertFalse(process_upload(upload))
            recalc.assert_not_called()
            upload.refresh_from_db()
            self.assertEqual(upload.status, 'REJECTED')
            self.assertTrue(ValidationIssue.objects.filter(run__upload=upload, code='STORED_FILE_HASH_MISMATCH', severity='P0').exists())
