from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from budgeting.models import BudgetCycle, NormalizedValue, Project, ProjectCycle, TemplateVersion, UploadVersion


class ReportBrowsingTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("browse-admin", role="ADMIN")
        self.cycle = BudgetCycle.objects.create(name="浏览测试", budget_year=2026, status="OPEN")
        template = TemplateVersion.objects.create(version="browse", budget_year=2026, file_path="missing.xlsx", manifest_path="missing.json", formula_manifest_hash="0" * 64)
        self.projects = []
        for code, amount in (("A", 100), ("B", 900)):
            project = Project.objects.create(code=code, name=f"酒店{code}")
            upload = UploadVersion.objects.create(project=project, cycle=self.cycle, template=template, status="APPROVED", sha256="1" * 64)
            ProjectCycle.objects.create(project=project, cycle=self.cycle, current_upload=upload)
            NormalizedValue.objects.create(upload=upload, report_code="A21前台", row_code="R0010", row_label="工资", period="01", unit="MONEY", value_int=amount, source_sheet="A21前台", source_cell="B10")
            self.projects.append(project)
        self.client.force_login(self.admin)

    def test_company_and_single_project_reuse_report_service(self):
        url = reverse("management_report", args=["A21前台"])
        self.assertEqual(self.client.get(url).context["values"][("R0010", "01")], 1000)
        page = self.client.get(url, {"project_id": self.projects[0].pk})
        self.assertEqual(page.context["values"][("R0010", "01")], 100)
        self.assertContains(page, "工资")
        self.assertContains(page, "酒店A")
        self.assertEqual(self.client.get(url, {"project_id": "bad"}).status_code, 404)

    def test_report_period_scope_defaults_to_budget_and_all_restores_source_periods(self):
        for project_cycle in ProjectCycle.objects.filter(cycle=self.cycle).select_related("current_upload"):
            upload = project_cycle.current_upload
            for month in range(2, 13):
                NormalizedValue.objects.create(
                    upload=upload, report_code="A21前台", row_code="R0010", row_label="工资",
                    period=f"{month:02d}", unit="MONEY", value_int=100,
                    source_sheet="A21前台", source_cell="B10",
                )
            for period in ("YEAR", "A2023M01", "A2024M01", "F2025M01"):
                NormalizedValue.objects.create(
                    upload=upload, report_code="A21前台", row_code="R0010", row_label="工资",
                    period=period, unit="MONEY", value_int=100,
                    source_sheet="A21前台", source_cell="B10",
                )

        url = reverse("management_report", args=["A21前台"])
        default_page = self.client.get(url)
        self.assertEqual(default_page.context["period_scope"], "budget")
        self.assertEqual(default_page.context["periods"], [*[f"{month:02d}" for month in range(1, 13)], "YEAR"])
        self.assertContains(default_page, "2026年预算1月")
        self.assertNotContains(default_page, "A2023M01")
        self.assertNotContains(default_page, "metric-card--progress")

        actual_page = self.client.get(url, {"period_scope": "t3_actual"})
        self.assertEqual(actual_page.context["periods"], ["A2023M01"])
        self.assertContains(actual_page, "2023年实际1月")

        all_page = self.client.get(url, {"period_scope": "all"})
        self.assertEqual(len(all_page.context["periods"]), 16)
        self.assertIn("A2023M01", all_page.context["periods"])
        self.assertIn("F2025M01", all_page.context["periods"])
        self.assertIn("YEAR", all_page.context["periods"])

    def test_project_account_cannot_change_scope_by_query(self):
        user = get_user_model().objects.create_user("browse-project", role="PROJECT", project=self.projects[0])
        self.client.force_login(user)
        page = self.client.get(reverse("project_report", args=["A21前台"]), {"project_id": self.projects[1].pk})
        self.assertEqual(page.context["values"][("R0010", "01")], 100)
        self.assertEqual(self.client.get(reverse("management_report", args=["A21前台"])).status_code, 404)

    def test_drilldown_preserves_selected_project_on_return(self):
        project = self.projects[0]
        url = reverse("management_report_drilldown", args=["A21前台"])
        page = self.client.get(url, {"project_id": project.pk, "row_code": "R0010", "period": "01"})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.context["contributions"]), 1)
        self.assertEqual(page.context["contributions"][0]["project"], project.code)
        report_url = reverse("management_report", args=["A21前台"])
        self.assertContains(page, f'href="{report_url}?project_id={project.pk}"', count=2)

    @patch("budgeting.views.sub_table_reports")
    def test_catalog_exposes_all_registered_sheets_even_without_values(self, tables):
        tables.return_value = [{"code": f"sheet{i}", "name": f"分表{i}", "rows": 0} for i in range(63)]
        page = self.client.get(reverse("report_catalog"))
        self.assertEqual(page.status_code, 200)
        for i in range(63):
            self.assertContains(page, f"分表{i}")
