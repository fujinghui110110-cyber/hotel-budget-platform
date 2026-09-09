from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from budgeting.models import (
    AdjustmentBatch,
    BudgetCycle,
    Project,
    ProjectCycle,
    TemplateVersion,
    UploadVersion,
)
from budgeting.services.budget_versions import (
    budget_version_rows,
    close_budget_version,
    create_and_open_budget_version,
    project_open_cycle,
    selected_project_upload,
)
from budgeting.services.workflow import issue_adjustment


class BudgetVersionServiceTests(TestCase):
    def setUp(self):
        self.template = TemplateVersion.objects.create(
            version="V3-2027",
            budget_year=2027,
            file_path="template.xlsx",
            manifest_path="manifest.json",
            formula_manifest_hash="a" * 64,
        )
        self.project = Project.objects.create(code="P01", name="一号酒店")
        self.other_project = Project.objects.create(code="P02", name="二号酒店")

    def test_opening_revision_closes_old_version_without_copying_uploads(self):
        old_cycle = BudgetCycle.objects.create(
            name="2027 年度预算",
            budget_year=2027,
            revision_no=1,
            status=BudgetCycle.Status.OPEN,
            template=self.template,
        )
        old_upload = UploadVersion.objects.create(
            project=self.project,
            cycle=old_cycle,
            template=self.template,
            status=UploadVersion.Status.APPROVED,
            original_name="old.xlsx",
            original_path="old/original.xlsx",
            sha256="b" * 64,
        )
        ProjectCycle.objects.create(
            project=self.project,
            cycle=old_cycle,
            current_upload=old_upload,
            is_open=True,
        )

        cycle = create_and_open_budget_version(budget_year=2027)

        old_cycle.refresh_from_db()
        self.assertEqual(old_cycle.status, BudgetCycle.Status.ADJUSTING)
        self.assertEqual(cycle.revision_no, 2)
        self.assertEqual(cycle.status, BudgetCycle.Status.OPEN)
        self.assertEqual(ProjectCycle.objects.filter(cycle=cycle, is_open=True).count(), 2)
        self.assertFalse(UploadVersion.objects.filter(cycle=cycle).exists())
        self.assertTrue(all(not row.has_uploaded for row in budget_version_rows(cycle)))

    def test_project_sees_only_its_open_version(self):
        hidden = BudgetCycle.objects.create(
            name="2027 年度预算",
            budget_year=2027,
            revision_no=1,
            status=BudgetCycle.Status.OPEN,
            template=self.template,
        )
        visible = BudgetCycle.objects.create(
            name="2028 年度预算",
            budget_year=2028,
            revision_no=1,
            status=BudgetCycle.Status.OPEN,
            template=self.template,
        )
        ProjectCycle.objects.create(project=self.project, cycle=hidden, is_open=False)
        ProjectCycle.objects.create(project=self.project, cycle=visible, is_open=True)

        self.assertEqual(project_open_cycle(self.project), visible)

        close_budget_version(visible)
        self.assertIsNone(project_open_cycle(self.project))

    def test_closing_legacy_adjusting_version_does_not_reopen_it(self):
        cycle = BudgetCycle.objects.create(
            name="2027 年度预算",
            budget_year=2027,
            revision_no=1,
            status=BudgetCycle.Status.ADJUSTING,
            template=self.template,
        )

        close_budget_version(cycle)

        self.assertIsNone(project_open_cycle(self.project))
        self.assertTrue(ProjectCycle.objects.filter(cycle=cycle, is_open=False).exists())

    def test_latest_failed_upload_is_shown_while_reports_use_latest_qualified_upload(self):
        cycle = create_and_open_budget_version(budget_year=2027)
        qualified = UploadVersion.objects.create(
            project=self.project,
            cycle=cycle,
            template=self.template,
            status=UploadVersion.Status.VALIDATED,
            original_name="qualified.xlsx",
            original_path="qualified/original.xlsx",
            sha256="c" * 64,
        )
        rejected = UploadVersion.objects.create(
            project=self.project,
            cycle=cycle,
            template=self.template,
            status=UploadVersion.Status.REJECTED,
            original_name="rejected.xlsx",
            original_path="rejected/original.xlsx",
            sha256="d" * 64,
        )

        row = next(item for item in budget_version_rows(cycle) if item.project == self.project)

        self.assertEqual(row.latest_upload, rejected)
        self.assertEqual(row.report_upload, qualified)
        self.assertEqual(selected_project_upload(self.project, cycle), qualified)

    def test_copied_reopen_baseline_does_not_count_as_project_upload(self):
        cycle = create_and_open_budget_version(budget_year=2027)
        UploadVersion.objects.create(
            project=self.project,
            cycle=cycle,
            template=self.template,
            status=UploadVersion.Status.APPROVED,
            original_name="baseline.xlsx",
            original_path="baseline/original.xlsx",
            sha256="e" * 64,
            note="由冻结周期 1 的版本 abc 复制为修订基线",
        )

        row = next(item for item in budget_version_rows(cycle) if item.project == self.project)

        self.assertFalse(row.has_uploaded)
        self.assertIsNone(row.report_upload)


class BudgetVersionProjectSecurityTests(TestCase):
    def setUp(self):
        self.template = TemplateVersion.objects.create(
            version="V3-2027-security",
            budget_year=2027,
            file_path="template.xlsx",
            manifest_path="manifest.json",
            formula_manifest_hash="f" * 64,
        )
        self.project = Project.objects.create(code="SEC01", name="安全测试酒店")
        self.user = get_user_model().objects.create_user(
            username="project-version-user",
            password="secret",
            role="PROJECT",
            project=self.project,
        )
        self.client.force_login(self.user)

    def test_project_cannot_submit_upload_from_closed_version(self):
        old_cycle = create_and_open_budget_version(budget_year=2027)
        upload = UploadVersion.objects.create(
            project=self.project,
            cycle=old_cycle,
            template=self.template,
            status=UploadVersion.Status.VALIDATED,
            original_name="old.xlsx",
            original_path="old/original.xlsx",
            sha256="1" * 64,
        )
        create_and_open_budget_version(budget_year=2027)

        response = self.client.post(reverse("project_submit_upload", args=[upload.pk]))

        self.assertEqual(response.status_code, 302)
        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadVersion.Status.VALIDATED)

    def test_project_can_open_upload_page_after_adjustment_is_issued(self):
        cycle = create_and_open_budget_version(budget_year=2027)
        batch = AdjustmentBatch.objects.create(
            cycle=cycle,
            project=self.project,
            report_code="PL_TOTAL_WINE",
            row_code="R0010",
            period="YEAR",
            baseline_total_cents=0,
            delta_cents=0,
            reason="下发后重新上传验证",
        )

        issue_adjustment(batch)
        cycle.refresh_from_db()
        response = self.client.get(reverse("project_upload_new"))

        self.assertEqual(cycle.status, BudgetCycle.Status.ADJUSTING)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2027 年预算 R1")

    def test_new_version_closes_previous_adjusting_version(self):
        old_cycle = create_and_open_budget_version(budget_year=2027)
        old_cycle.status = BudgetCycle.Status.ADJUSTING
        old_cycle.save(update_fields=["status"])

        new_cycle = create_and_open_budget_version(budget_year=2027)

        self.assertFalse(
            ProjectCycle.objects.get(project=self.project, cycle=old_cycle).is_open
        )
        self.assertEqual(project_open_cycle(self.project), new_cycle)


class BudgetVersionPageTests(TestCase):
    def setUp(self):
        self.template = TemplateVersion.objects.create(
            version="V3-2027-page",
            budget_year=2027,
            file_path="template.xlsx",
            manifest_path="manifest.json",
            formula_manifest_hash="2" * 64,
        )
        self.project = Project.objects.create(code="PAGE01", name="页面测试酒店")
        self.admin = get_user_model().objects.create_user(
            username="version-page-admin",
            password="secret",
            role="ADMIN",
        )
        self.project_user = get_user_model().objects.create_user(
            username="version-page-project",
            password="secret",
            role="PROJECT",
            project=self.project,
        )

    def test_admin_page_shows_selected_version_project_status_and_batch_export(self):
        cycle = create_and_open_budget_version(budget_year=2027)
        self.client.force_login(self.admin)

        response = self.client.get(reverse("management_budget_versions"), {"cycle": cycle.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2027 年预算 R1")
        self.assertContains(response, "PAGE01 页面测试酒店")
        self.assertContains(response, "未上传")
        self.assertContains(response, "一键导出全部项目预算报表")

    def test_project_account_cannot_open_management_version_page(self):
        create_and_open_budget_version(budget_year=2027)
        self.client.force_login(self.project_user)

        response = self.client.get(reverse("management_budget_versions"))

        self.assertEqual(response.status_code, 404)


class RehearsalVersionBoundaryTests(TestCase):
    def test_rehearsal_is_explicit_and_project_cannot_choose_source_year(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from budgeting.forms import UploadForm
        template = TemplateVersion.objects.create(version='V3-2030', budget_year=2030)
        cycle = create_and_open_budget_version(budget_year=2030, source_budget_year=2029, template=template)
        self.assertEqual(cycle.source_budget_year, 2029)
        file = SimpleUploadedFile('legacy.xlsm', b'workbook')
        self.assertTrue(UploadForm(files={'file': file}, cycle=cycle).is_valid())
        standard = create_and_open_budget_version(budget_year=2030, template=template)
        self.assertIsNone(standard.source_budget_year)
        self.assertFalse(UploadForm(data={'source_budget_year': '2029'}, files={'file': file}, cycle=standard).is_valid())
        with self.assertRaisesMessage(ValueError, '演练原表年度'):
            create_and_open_budget_version(budget_year=2030, source_budget_year=2031, template=template)
