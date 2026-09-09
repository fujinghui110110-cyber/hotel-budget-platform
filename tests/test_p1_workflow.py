from django.test import Client, TestCase, override_settings
from django.urls import reverse

from budgeting.models import AuditEvent, BudgetCycle, Project, UploadVersion, User, ValidationIssue, ValidationRun


@override_settings(ROOT_URLCONF="budgeting.issue_urls")
class P1WorkflowTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(code="P1A", name="P1 项目 A")
        self.other_project = Project.objects.create(code="P1B", name="P1 项目 B")
        self.cycle = BudgetCycle.objects.create(name="2027 预算", budget_year=2027, status=BudgetCycle.Status.OPEN)
        self.upload = UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            original_name="budget.xlsx",
            original_path="P1A/budget.xlsx",
            sha256="0" * 64,
        )
        self.other_upload = UploadVersion.objects.create(
            project=self.other_project,
            cycle=self.cycle,
            original_name="other.xlsx",
            original_path="P1B/other.xlsx",
            sha256="1" * 64,
        )
        self.run = ValidationRun.objects.create(upload=self.upload, rule_version="R1")
        self.other_run = ValidationRun.objects.create(upload=self.other_upload, rule_version="R1")
        self.issue = ValidationIssue.objects.create(
            run=self.run,
            severity=ValidationIssue.Severity.P1,
            code="AMOUNT_VARIANCE",
            message="金额变动超过阈值",
            location="损益表!A1",
            actual_value="200",
            expected_value="100",
        )
        self.other_issue = ValidationIssue.objects.create(
            run=self.other_run,
            severity=ValidationIssue.Severity.P1,
            code="AMOUNT_VARIANCE",
            message="其他项目问题",
        )
        self.project_user = User.objects.create_user(
            username="p1-project",
            password="test-password",
            role=User.Role.PROJECT,
            project=self.project,
        )
        self.other_user = User.objects.create_user(
            username="p1-other",
            password="test-password",
            role=User.Role.PROJECT,
            project=self.other_project,
        )
        self.admin = User.objects.create_user(
            username="p1-admin",
            password="test-password",
            role=User.Role.ADMIN,
        )

    def issue_url(self, issue=None):
        return reverse("issue_explanation", kwargs={"issue_id": (issue or self.issue).pk})

    def threshold_url(self):
        return reverse("management_p1_threshold", kwargs={"cycle_id": self.cycle.pk})

    def test_project_must_submit_non_blank_explanation_and_is_not_acknowledged(self):
        self.client.force_login(self.project_user)
        response = self.client.post(self.issue_url(), {"acknowledgement_note": "   "})
        self.assertEqual(response.status_code, 400)
        self.issue.refresh_from_db()
        self.assertFalse(self.issue.acknowledged)
        self.assertEqual(self.issue.acknowledgement_note, "")
        self.assertFalse(AuditEvent.objects.filter(action="P1_ISSUE_EXPLAINED").exists())

        response = self.client.post(self.issue_url(), {"acknowledgement_note": "已核对源文件，预算变动有业务依据。"})
        self.assertEqual(response.status_code, 302)
        self.issue.refresh_from_db()
        self.assertFalse(self.issue.acknowledged)
        self.assertEqual(self.issue.acknowledgement_note, "已核对源文件，预算变动有业务依据。")
        self.assertTrue(AuditEvent.objects.filter(action="P1_ISSUE_EXPLAINED", upload=self.upload).exists())

    def test_project_cannot_post_other_project_issue(self):
        self.client.force_login(self.project_user)
        response = self.client.post(
            self.issue_url(self.other_issue),
            {"acknowledgement_note": "越权说明"},
        )
        self.assertEqual(response.status_code, 403)
        self.other_issue.refresh_from_db()
        self.assertEqual(self.other_issue.acknowledgement_note, "")

    def test_admin_can_confirm_existing_explanation_without_retyping_it(self):
        self.issue.acknowledgement_note = "项目端已提交的说明。"
        self.issue.save(update_fields=["acknowledgement_note"])
        self.client.force_login(self.admin)
        response = self.client.post(self.issue_url(), {})
        self.assertEqual(response.status_code, 302)
        self.issue.refresh_from_db()
        self.assertTrue(self.issue.acknowledged)
        self.assertEqual(self.issue.acknowledgement_note, "项目端已提交的说明。")
        self.assertTrue(AuditEvent.objects.filter(action="P1_ISSUE_ACKNOWLEDGED", upload=self.upload).exists())

    def test_admin_cannot_confirm_without_existing_explanation(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.issue_url(), {})
        self.assertEqual(response.status_code, 400)
        self.issue.refresh_from_db()
        self.assertFalse(self.issue.acknowledged)
        self.assertFalse(AuditEvent.objects.filter(action="P1_ISSUE_ACKNOWLEDGED").exists())

    def test_frozen_issue_is_read_only(self):
        self.issue.acknowledgement_note = "冻结前说明"
        self.issue.save(update_fields=["acknowledgement_note"])
        self.cycle.status = BudgetCycle.Status.FROZEN
        self.cycle.save(update_fields=["status"])
        self.client.force_login(self.project_user)
        response = self.client.post(self.issue_url(), {"acknowledgement_note": "不应写入"})
        self.assertEqual(response.status_code, 409)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.acknowledgement_note, "冻结前说明")
        self.assertFalse(AuditEvent.objects.filter(action="P1_ISSUE_EXPLAINED").exists())

    def test_threshold_accepts_decimal_yuan_and_blank_disables_it(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.threshold_url(), {"threshold_yuan": "12.345"})
        self.assertEqual(response.status_code, 302)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.p1_threshold_cents, 1235)
        self.assertTrue(AuditEvent.objects.filter(action="P1_THRESHOLD_UPDATED", cycle=self.cycle).exists())

        response = self.client.post(self.threshold_url(), {"threshold_yuan": ""})
        self.assertEqual(response.status_code, 302)
        self.cycle.refresh_from_db()
        self.assertIsNone(self.cycle.p1_threshold_cents)

    def test_threshold_rejects_negative_project_user_and_frozen_cycle(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.threshold_url(), {"threshold_yuan": "-1"})
        self.assertEqual(response.status_code, 400)
        self.cycle.refresh_from_db()
        self.assertIsNone(self.cycle.p1_threshold_cents)

        self.client.force_login(self.project_user)
        self.assertEqual(self.client.get(self.threshold_url()).status_code, 403)

        self.client.force_login(self.admin)
        self.cycle.status = BudgetCycle.Status.FROZEN
        self.cycle.save(update_fields=["status"])
        response = self.client.post(self.threshold_url(), {"threshold_yuan": "10"})
        self.assertEqual(response.status_code, 409)
        self.cycle.refresh_from_db()
        self.assertIsNone(self.cycle.p1_threshold_cents)

    def test_csrf_is_required_for_issue_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.project_user)
        response = client.get(self.issue_url())
        self.assertEqual(response.status_code, 200)
        response = client.post(self.issue_url(), {"acknowledgement_note": "无 CSRF"})
        self.assertEqual(response.status_code, 403)

