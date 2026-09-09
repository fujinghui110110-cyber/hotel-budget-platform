from django.test import TestCase

from budgeting.models import BudgetCycle, NormalizedValue, Project, ProjectCycle, UploadVersion
from budgeting.services.workflow import approve_upload, create_adjustment_batch, issue_adjustment


class ApprovalV2Tests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(code="REAL", name="版本保护")
        self.cycle = BudgetCycle.objects.create(name="2027", budget_year=2027, status="OPEN")
        self.old = UploadVersion.objects.create(project=self.project, cycle=self.cycle, status="APPROVED")
        self.pc = ProjectCycle.objects.create(project=self.project, cycle=self.cycle, current_upload=self.old)
        self.new = UploadVersion.objects.create(project=self.project, cycle=self.cycle, status="SUBMITTED")
        for upload, cents in [(self.old, 10000), (self.new, 10099)]:
            NormalizedValue.objects.create(upload=upload, report_code="PL_TOTAL_WINE", row_code="R0041", period="01", unit="MONEY", value_int=cents)

    def test_one_cent_short_preserves_official_version(self):
        batch = create_adjustment_batch(self.cycle, "PL_TOTAL_WINE", "R0041", "01", 100, "修订")
        issue_adjustment(batch)
        with self.assertRaisesMessage(ValueError, "精确落实"):
            approve_upload(self.new)
        self.pc.refresh_from_db()
        self.assertEqual(self.pc.current_upload_id, self.old.pk)
        NormalizedValue.objects.filter(upload=self.new).update(value_int=10100)
        approve_upload(self.new)
        self.pc.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(self.pc.current_upload_id, self.new.pk)
        self.assertEqual(batch.status, "COMPLETED")

    def test_frozen_cycle_is_read_only(self):
        self.cycle.status = "FROZEN"
        self.cycle.save()
        with self.assertRaisesMessage(ValueError, "冻结周期"):
            approve_upload(self.new)
