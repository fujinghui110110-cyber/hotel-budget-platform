from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from budgeting.models import AdjustmentBatch, BudgetScenario
from tests import test_summary_scenarios as fixtures


class SummaryViewTests(TestCase):
    def setUp(self):
        fixtures.SummaryScenarioTests.setUp(self)
        self.client.force_login(self.admin)

    def _seed_values(self):
        fixtures.SummaryScenarioTests._seed_values(self)

    def test_admin_edits_annual_table_then_project_sees_changes(self):
        response = self.client.post(reverse("summary_list"), {
            "cycle": self.cycle.pk, "project": self.project.pk, "report": "PL_TOTAL_WINE",
        })
        self.assertEqual(response.status_code, 302)
        url = response.url
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        payload = {f"value_{row['code']}": row["input_value"] for row in page.context["rows"] if row["editable"]}
        payload.update(action="calculate", reason="演示验收：提高年度客房收入")
        payload["value_R0041"] = str(Decimal(payload["value_R0041"].replace(",", "")) + 1000)
        saved = self.client.post(url, payload)
        self.assertEqual(saved.status_code, 302)
        scenario = BudgetScenario.objects.get()
        self.assertEqual(scenario.status, "READY")
        self.assertTrue(scenario.results["changed_rows"])
        payload["action"] = "issue"
        payload["value_R0041"] = str(Decimal(payload["value_R0041"]) + 1)
        rejected = self.client.post(url, payload)
        self.assertContains(rejected, "存在未保存的改动")
        self.assertEqual(AdjustmentBatch.objects.count(), 0)
        payload["value_R0041"] = str(Decimal(payload["value_R0041"]) - 1)
        issued = self.client.post(url, payload)
        self.assertEqual(issued.status_code, 302)
        self.assertEqual(AdjustmentBatch.objects.count(), 1)
        self.client.force_login(self.project_user)
        project_page = self.client.get(reverse("project_adjustments"))
        self.assertContains(project_page, "重点改动")
        self.assertContains(project_page, "提高年度客房收入")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(reverse("summary_list"), {"cycle": self.cycle.pk}).status_code, 404)
