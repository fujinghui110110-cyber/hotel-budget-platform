from django.test import TestCase

from budgeting.models import BudgetCycle, NormalizedValue, Project, ProjectCycle, UploadVersion, TemplateVersion, User
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
        from budgeting.services.plan_history import ensure_plan
        from budgeting.services.targets import issue_targets
        plan = ensure_plan(self.cycle)
        admin = User.objects.create_user(username="approval-admin", role="ADMIN")
        template = TemplateVersion.objects.create(version="approval-test", budget_year=2027,
            formula_manifest_hash="a" * 64, rule_version="test-v1")
        issue_targets(actor=admin, plan=plan, project=self.project, origin_cycle=self.cycle,
            expected_plan_revision=plan.revision_token, reason="明确客房收入最低要求",
            selected_target_rows=[{"report_code": "PL_TOTAL_WINE", "row_code": "R0041", "period": "01",
                "unit": "CNY_CENT", "metric_kind": "REVENUE", "comparator": "GE", "target_int": 10100,
                "sign_multiplier": 1, "evidence": "管理员选定收入要求", "rule_version": "test-v1"}])
        self.new = UploadVersion.objects.create(project=self.project, cycle=self.cycle,
            template=template, status="SUBMITTED")
        NormalizedValue.objects.create(upload=self.new, report_code="PL_TOTAL_WINE", row_code="R0041",
            period="01", data_year=2027, data_kind="BUDGET", unit="MONEY", value_int=10099)
        with self.assertRaisesMessage(ValueError, "TARGET_UNMET"):
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

    def test_withdrawing_reviewed_target_does_not_revive_legacy_constraint(self):
        from budgeting.models import User
        from budgeting.services.plan_history import ensure_plan
        from budgeting.services.targets import issue_targets
        batch = create_adjustment_batch(self.cycle, "PL_TOTAL_WINE", "R0041", "01", 100, "旧下发")
        issue_adjustment(batch)
        plan = ensure_plan(self.cycle)
        admin = User.objects.create_user(username="withdraw-admin", role="ADMIN")
        issue_targets(actor=admin, plan=plan, project=self.project, origin_cycle=self.cycle,
            expected_plan_revision=plan.revision_token, reason="复核旧收入要求",
            selected_target_rows=[{"report_code": "PL_TOTAL_WINE", "row_code": "R0041", "period": "01",
                "unit": "CNY_CENT", "metric_kind": "REVENUE", "comparator": "GE", "target_int": 10100,
                "sign_multiplier": 1, "evidence": "管理员明确接管旧要求", "rule_version": "test-v1"}])
        plan.refresh_from_db()
        issue_targets(actor=admin, plan=plan, project=self.project, origin_cycle=self.cycle,
            expected_plan_revision=plan.revision_token, reason="业务依据取消收入要求",
            selected_target_rows=[], revoke=True)
        approve_upload(self.new, actor=admin)
        self.pc.refresh_from_db()
        self.assertEqual(self.pc.current_upload_id, self.new.pk)
