import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from budgeting.models import (
    AdjustmentBatch,
    AdjustmentLine,
    BudgetCycle,
    FreezeSnapshot,
    Project,
    ProjectCycle,
    UploadVersion,
)
from budgeting.services.files import sha256_file
from budgeting.services.workflow import (
    cancel_adjustment,
    create_adjustment_batch,
    create_driver_adjustment,
    create_full_adjustment,
    freeze_cycle,
    issue_adjustment,
    override_adjustment_line,
    reject_upload,
    update_adjustment_lines_for_upload,
)


class FreezeSafetyV2Tests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = self.root / "storage"
        self.storage.mkdir()
        self.settings = override_settings(
            BASE_DIR=self.root,
            BUDGET_STORAGE_ROOT=self.storage,
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)

        self.project = Project.objects.create(code="P001", name="项目一")
        self.cycle = BudgetCycle.objects.create(
            name="2026预算", budget_year=2026, status=BudgetCycle.Status.OPEN
        )
        self.original_rel = Path("uploads/P001/original.xlsx")
        self.original = self.storage / self.original_rel
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(b"original-A")
        self.upload = UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            status=UploadVersion.Status.APPROVED,
            original_name="original.xlsx",
            original_path=str(self.original_rel),
            sha256=sha256_file(self.original),
        )
        ProjectCycle.objects.create(
            project=self.project,
            cycle=self.cycle,
            current_upload=self.upload,
        )

    def _freeze(self):
        with patch("budgeting.services.workflow._write_company_report_xlsx"), patch(
            "budgeting.services.workflow._write_adjustment_ledger_xlsx"
        ), patch("budgeting.services.workflow._write_validation_report_xlsx"):
            return freeze_cycle(self.cycle)

    def test_frozen_cycle_rejects_every_adjustment_write_entry(self):
        self.cycle.status = BudgetCycle.Status.FROZEN
        self.cycle.save(update_fields=["status"])

        with self.assertRaisesMessage(ValueError, "冻结周期"):
            create_adjustment_batch(
                self.cycle, "PL_TOTAL_WINE", "R0041", "01", 1, "修订"
            )
        with self.assertRaisesMessage(ValueError, "冻结周期"):
            create_driver_adjustment(
                self.cycle, self.project, "PL_TOTAL_WINE", "OCC", 72, "修订"
            )
        with self.assertRaisesMessage(ValueError, "冻结周期"):
            create_full_adjustment(
                self.cycle, self.project, "PL_TOTAL_WINE", {"R0041": {"YEAR": 1}}, "修订"
            )

        batch = AdjustmentBatch.objects.create(
            cycle=self.cycle,
            project=self.project,
            report_code="PL_TOTAL_WINE",
            row_code="R0041",
            period="01",
            delta_cents=1,
            reason="修订",
        )
        line = AdjustmentLine.objects.create(
            batch=batch,
            cycle=self.cycle,
            project=self.project,
            report_code="PL_TOTAL_WINE",
            row_code="R0041",
            period="01",
            target_cents=1,
        )
        for action in (
            lambda: issue_adjustment(batch),
            lambda: cancel_adjustment(batch),
            lambda: override_adjustment_line(line, 1),
            lambda: update_adjustment_lines_for_upload(self.upload),
        ):
            with self.assertRaisesMessage(ValueError, "冻结周期"):
                action()

        submitted = UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            status=UploadVersion.Status.SUBMITTED,
            original_name="submitted.xlsx",
            original_path=str(self.original_rel),
            sha256=self.upload.sha256,
        )
        with self.assertRaisesMessage(ValueError, "冻结周期"):
            reject_upload(submitted, reason="退回")

    def test_repeated_freeze_rejects_without_second_complete_snapshot(self):
        snapshot = self._freeze()
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, BudgetCycle.Status.FROZEN)
        self.assertEqual(snapshot.status, FreezeSnapshot.Status.COMPLETE)

        with self.assertRaisesMessage(ValueError, "已冻结"):
            freeze_cycle(self.cycle)

        self.assertEqual(
            FreezeSnapshot.objects.filter(
                cycle=self.cycle, status=FreezeSnapshot.Status.COMPLETE
            ).count(),
            1,
        )

    def test_freeze_rejects_original_file_tampering_before_snapshot_completion(self):
        self.original.write_bytes(b"original-B")

        with self.assertRaisesMessage(ValueError, "哈希不匹配"):
            self._freeze()

        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, BudgetCycle.Status.OPEN)
        self.assertFalse(
            FreezeSnapshot.objects.filter(
                cycle=self.cycle, status=FreezeSnapshot.Status.COMPLETE
            ).exists()
        )
        self.assertTrue(
            FreezeSnapshot.objects.filter(
                cycle=self.cycle, status=FreezeSnapshot.Status.FAILED
            ).exists()
        )
