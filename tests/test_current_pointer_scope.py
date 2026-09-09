from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from budgeting.models import (
    BudgetCycle,
    NormalizedValue,
    Project,
    ProjectCycle,
    TemplateVersion,
    UploadVersion,
)
from budgeting.services.workflow import (
    _company_value_details,
    project_contributions,
    project_trend_rows,
    project_value_details,
)
from budgeting.views import _dashboard_data


class CurrentPointerScopeTests(TestCase):
    report_code = "PL_TOTAL_WINE"
    row_code = "ROOM"

    def setUp(self):
        self.project_a = Project.objects.create(code="P001", name="项目一")
        self.project_b = Project.objects.create(code="P002", name="项目二")
        self.template = TemplateVersion.objects.create(
            version="V1",
            budget_year=2026,
            file_path="template.xlsx",
            manifest_path="manifest.json",
            formula_manifest_hash="0" * 64,
        )
        self.cycle_a = BudgetCycle.objects.create(
            name="2026预算",
            budget_year=2026,
            status=BudgetCycle.Status.OPEN,
            template=self.template,
        )
        self.cycle_b = BudgetCycle.objects.create(
            name="2025预算",
            budget_year=2025,
            status=BudgetCycle.Status.OPEN,
            template=self.template,
        )

    def _upload(self, project, cycle, value_int=0):
        upload = UploadVersion.objects.create(
            project=project,
            cycle=cycle,
            template=self.template,
            status=UploadVersion.Status.APPROVED,
            original_path=f"{project.code}-{cycle.budget_year}.xlsx",
            sha256="0" * 64,
        )
        NormalizedValue.objects.create(
            upload=upload,
            report_code=self.report_code,
            row_code=self.row_code,
            row_label="客房收入",
            period="01",
            unit=NormalizedValue.Unit.MONEY,
            value_int=value_int,
            source_sheet="损益",
            source_cell="A1",
        )
        return upload

    def _report_context(self):
        admin = Client()
        user = get_user_model().objects.create_user(
            username="admin", password="x", role="ADMIN", is_staff=True
        )
        admin.force_login(user)
        report = admin.get(reverse("management_report", args=[self.report_code]))
        drilldown = admin.get(
            reverse("management_report_drilldown", args=[self.report_code]),
            {"row_code": self.row_code, "period": "01"},
        )
        return report, drilldown

    def test_cross_cycle_pointer_is_excluded_from_report_and_drilldown(self):
        upload = self._upload(self.project_a, self.cycle_b, value_int=900)
        ProjectCycle.objects.create(
            project=self.project_a, cycle=self.cycle_a, current_upload=upload
        )

        self.assertEqual(_company_value_details(self.cycle_a, self.report_code), {})
        self.assertEqual(
            project_value_details(self.project_a, self.cycle_a, self.report_code), {}
        )
        self.assertEqual(project_trend_rows(self.cycle_a, self.report_code, self.row_code), [])
        self.assertEqual(
            list(project_contributions(self.cycle_a, self.report_code, self.row_code)), []
        )
        self.assertEqual(_dashboard_data(self.cycle_a, self.report_code), ([], []))

        report, drilldown = self._report_context()
        self.assertEqual(report.context["values"], {})
        self.assertEqual(drilldown.context["contributions"], [])

    def test_cross_project_pointer_is_excluded_from_summary_trend_and_drilldown(self):
        upload = self._upload(self.project_b, self.cycle_a, value_int=900)
        ProjectCycle.objects.create(
            project=self.project_a, cycle=self.cycle_a, current_upload=upload
        )

        self.assertEqual(_company_value_details(self.cycle_a, self.report_code), {})
        self.assertEqual(
            project_value_details(self.project_a, self.cycle_a, self.report_code), {}
        )
        self.assertEqual(project_trend_rows(self.cycle_a, self.report_code, self.row_code), [])
        self.assertEqual(
            list(project_contributions(self.cycle_a, self.report_code, self.row_code)), []
        )
        self.assertEqual(_dashboard_data(self.cycle_a, self.report_code), ([], []))

        report, drilldown = self._report_context()
        self.assertEqual(report.context["values"], {})
        self.assertEqual(drilldown.context["contributions"], [])

    def test_matching_active_pointer_is_included(self):
        upload = self._upload(self.project_a, self.cycle_a, value_int=123)
        ProjectCycle.objects.create(
            project=self.project_a, cycle=self.cycle_a, current_upload=upload
        )

        details = _company_value_details(self.cycle_a, self.report_code)
        self.assertEqual(details[(self.row_code, "01")]["value_int"], 123)
        self.assertEqual(
            project_value_details(self.project_a, self.cycle_a, self.report_code), details
        )
        rows = project_trend_rows(self.cycle_a, self.report_code, self.row_code)
        self.assertEqual([row["code"] for row in rows], [self.project_a.code])
        self.assertEqual(rows[0]["series"]["01"]["value_int"], 123)
        contributions = list(
            project_contributions(self.cycle_a, self.report_code, self.row_code)
        )
        self.assertEqual(len(contributions), 1)
        self.assertEqual(contributions[0].value_int, 123)

    def test_inactive_project_pointer_is_excluded(self):
        inactive = Project.objects.create(code="P003", name="停用项目", is_active=False)
        upload = self._upload(inactive, self.cycle_a, value_int=900)
        ProjectCycle.objects.create(
            project=inactive, cycle=self.cycle_a, current_upload=upload
        )

        self.assertEqual(_company_value_details(self.cycle_a, self.report_code), {})
        self.assertEqual(project_trend_rows(self.cycle_a, self.report_code, self.row_code), [])
        self.assertEqual(
            list(project_contributions(self.cycle_a, self.report_code, self.row_code)), []
        )
