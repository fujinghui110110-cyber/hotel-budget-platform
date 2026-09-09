from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from budgeting.models import BudgetCycle, NormalizedValue, Project, ProjectCycle, TemplateVersion, UploadVersion
from budgeting.services.trends import build_drilldown, build_trend


class ReadScopeConsistencyTests(TestCase):
    def setUp(self):
        self.cycle = BudgetCycle.objects.create(name="预算", budget_year=2027, status="OPEN")
        self.template = TemplateVersion.objects.create(version="T", budget_year=2027, file_path="x", manifest_path="m", formula_manifest_hash="0" * 64)
        self.project = Project.objects.create(code="P1", name="项目一")
        self.other = Project.objects.create(code="P2", name="项目二")
        self.admin = get_user_model().objects.create_user(username="admin", role="ADMIN", is_staff=True)
        self.user = get_user_model().objects.create_user(username="project", role="PROJECT", project=self.other)
        self.upload = self.make_upload("SUBMITTED")
        ProjectCycle.objects.create(project=self.project, cycle=self.cycle)
        self.value = NormalizedValue.objects.create(upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0032", period="YEAR", data_year=2027, data_kind="BUDGET", unit="MONEY", value_int=123456, source_sheet="汇总", source_cell="J32")
        NormalizedValue.objects.create(upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0032", period="01", data_year=2027, data_kind="BUDGET", month=1, unit="MONEY", value_int=12000)

    def make_upload(self, status):
        return UploadVersion.objects.create(project=self.project, cycle=self.cycle, template=self.template, status=status, original_path="budget.xlsx", sha256="a" * 64)

    def test_submitted_scope_and_failed_retry_preserve_visible_data(self):
        self.make_upload("REJECTED")
        trend = build_trend(self.cycle, data_scope="latest")
        budget = next(item for item in trend["series"] if item["kind"] == "BUDGET")
        self.assertEqual(budget["values"][0], 12000)
        details = build_drilldown(self.cycle, data_scope="latest")
        self.assertEqual(details["projects"][0]["value"], 123456)
        self.assertEqual(details["projects"][0]["upload_id"], str(self.upload.pk))
        self.assertEqual(build_drilldown(self.cycle)["projects"], [])

    def test_all_cockpit_read_surfaces_include_submitted_upload(self):
        self.client.force_login(self.admin)
        params = {"cycle": self.cycle.pk, "project_id": self.project.pk}
        for name in ("cockpit_dashboard", "cockpit_trend", "cockpit_trend_data", "cockpit_export", "cockpit_drilldown"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name), params)
                self.assertEqual(response.status_code, 200)
        response = self.client.get(reverse("cockpit_trend_data"), params)
        self.assertEqual(response.json()["project_ids"], [self.project.pk])
        response = self.client.get(reverse("cockpit_drilldown"), {**params, "format": "json"})
        self.assertEqual(response.json()["projects"][0]["value"], 123456)
        response = self.client.get(reverse("cockpit_project", args=[self.project.pk]), params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["project_cycle"].current_upload_id, self.upload.pk)

    def test_project_user_cannot_access_management_data_or_other_source(self):
        self.upload.status = "APPROVED"
        self.upload.save(update_fields=["status"])
        ProjectCycle.objects.filter(cycle=self.cycle, project=self.project).update(current_upload=self.upload)
        self.client.force_login(self.user)
        for name in ("cockpit_dashboard", "cockpit_trend_data", "cockpit_drilldown", "cockpit_export"):
            self.assertEqual(self.client.get(reverse(name), {"cycle": self.cycle.pk}).status_code, 403)
        response = self.client.get(reverse("cockpit_question_new"), {"value_id": self.value.pk})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["context"])
