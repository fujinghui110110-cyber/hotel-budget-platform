from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from budgeting.models import BudgetCycle, IndicatorProject, Project, User
from budgeting.management_metric_views import _preview_rows


class ManagementMetricViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="metric_admin", role="ADMIN")
        self.project = Project.objects.create(code="MVIEW", name="指标测试酒店")
        self.identity = IndicatorProject.objects.create(name="指标测试酒店", project=self.project)
        self.cycle = BudgetCycle.objects.create(name="2027 R1", budget_year=2027)

    @patch("budgeting.management_metric_views.service.comparison_data")
    def test_comparison_passes_compare_index_and_formats_periods(self, comparison):
        comparison.return_value = {
            "rows": [
                {
                    "name": "指标测试酒店",
                    "project_id": self.identity.pk,
                    "values": [100, 120, None, 180],
                    "delta": 60,
                    "growth": 50,
                }
            ],
            "totals": {"values": [100, 120, None, 180], "delta": 60, "growth": 50},
            "periods": ["2024 实际", "2025 实际", "2026 预测", "2027 预算"],
            "metric_label": "收入合计",
            "unit": "MONEY",
            "trend": [],
            "notes": [],
            "comparison_label": "2025 实际",
        }
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("management_metrics"),
            {
                "cycle": self.cycle.pk,
                "report_code": "PL_TOTAL_NOWINE",
                "metric": "revenue_total",
                "compare_index": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(comparison.call_args.kwargs["compare_index"], 1)
        self.assertContains(response, "预算较2025 实际")
        self.assertContains(response, "180")

    @patch("budgeting.management_metric_views.service.comparison_data")
    def test_project_user_cannot_select_another_indicator_project(self, comparison):
        other_project = Project.objects.create(code="OTHER", name="其他酒店")
        other_identity = IndicatorProject.objects.create(name="其他酒店", project=other_project)
        project_user = User.objects.create_user(username="metric_project", role="PROJECT", project=self.project)
        comparison.return_value = {
            "rows": [],
            "totals": {},
            "periods": [],
            "metric_label": "收入合计",
            "unit": "MONEY",
            "trend": [],
        }
        self.client.force_login(project_user)
        response = self.client.get(reverse("management_metrics"), {"project": other_identity.pk})
        self.assertEqual(response.status_code, 404)
        comparison.assert_not_called()

    @patch("budgeting.management_metric_views.service.comparison_data")
    def test_admin_can_filter_transient_negative_budget_project(self, comparison):
        comparison.return_value = {
            "rows": [],
            "totals": {},
            "periods": [],
            "metric_label": "收入合计",
            "unit": "MONEY",
            "trend": [],
        }
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("management_metrics"),
            {"project": f"-{self.project.pk}", "metric": "revenue_total"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(comparison.call_args.kwargs["project_id"], -self.project.pk)

    def test_preview_formats_stored_yuan_and_ratio_values(self):
        rows = _preview_rows(
            {
                "money_unit": "WAN",
                "rows": [
                    {"project_name": "A", "metric": "revenue_total", "value": "1234567"},
                    {"project_name": "A", "metric": "occ", "unit": "RATIO", "value": "0.5"},
                    {"project_name": "A", "metric": "adr", "value": "888.88"},
                ],
            }
        )
        self.assertEqual([row["value_display"] for row in rows], ["123", "50.00%", "888.88"])

    @patch("budgeting.management_metric_views.service.comparison_data")
    def test_missing_cycle_defaults_to_latest_round_for_selected_year(self, comparison):
        comparison.return_value = {
            "rows": [],
            "totals": {},
            "periods": [],
            "metric_label": "收入合计",
            "unit": "MONEY",
            "trend": [],
        }
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("management_metrics"),
            {"year": "2027", "metric": "revenue_total"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(comparison.call_args.kwargs["cycle"].pk, self.cycle.pk)
        self.assertFalse(response.context["cycle_unbound"])

        response = self.client.get(
            reverse("management_metrics"),
            {"year": "2027", "metric": "revenue_total", "cycle": "unbound"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(comparison.call_args.kwargs["cycle"])
        self.assertTrue(response.context["cycle_unbound"])

    @patch("budgeting.management_metric_views.service.comparison_data")
    def test_page_explains_unit_notes_and_coverage_and_hides_ratio_growth(self, comparison):
        comparison.return_value = {
            "rows": [{"name": "指标测试酒店", "values": [0.5, 0.6, 0.7, 0.8], "delta": 0.1, "growth": None}],
            "totals": {"values": [0.5, 0.6, 0.7, 0.8], "delta": 0.1, "growth": None, "coverage": [18, 18, 18, 3], "expected": 18},
            "periods": ["2024 实际", "2025 实际", "2026 预测", "2027 预算"],
            "metric_label": "出租率",
            "unit": "RATIO",
            "trend": [],
            "notes": ["缺数不自动补零。"],
            "comparison_label": "2026 预测",
        }
        self.client.force_login(self.admin)
        response = self.client.get(reverse("management_metrics"), {"cycle": self.cycle.pk, "metric": "occ"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "单位：百分比")
        self.assertContains(response, "口径说明：")
        self.assertContains(response, "2027 预算 3/18")
        self.assertNotContains(response, "增长百分比")

    def test_project_user_cannot_import_or_download(self):
        project_user = User.objects.create_user(username="metric_project", role="PROJECT", project=self.project)
        self.client.force_login(project_user)
        self.assertEqual(self.client.get(reverse("management_metric_import")).status_code, 403)
        self.assertEqual(self.client.get(reverse("management_metric_source", args=[1])).status_code, 403)

    @patch("budgeting.management_metric_views.service.confirm_import")
    @patch("budgeting.management_metric_views.service.preview_import")
    @patch("budgeting.management_metric_views.service.latest_batch_id")
    def test_admin_preview_then_confirm_uses_owner_token_and_reason(
        self, latest_batch_id, preview_import, confirm_import
    ):
        staging = Path(tempfile.mkdtemp(prefix="budget-metric-view-"))
        settings_override = override_settings(BUDGET_STORAGE_ROOT=str(staging))
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.addCleanup(lambda: shutil.rmtree(staging, ignore_errors=True))
        latest_batch_id.return_value = 0
        preview_import.return_value = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "rows": [
                {
                    "project_name": "指标测试酒店",
                    "metric": "revenue_total",
                    "year": 2025,
                    "month": 0,
                    "value": "120",
                    "source_sheet": "收入",
                    "source_cell": "N4",
                }
            ],
            "projects": ["指标测试酒店"],
            "years": [2025],
            "sha256": "a" * 64,
            "unlinked_projects": [],
        }
        confirm_import.return_value = SimpleNamespace(pk=1)
        self.client.force_login(self.admin)
        uploaded = SimpleUploadedFile("history.xlsx", b"fake-xlsx")
        response = self.client.post(
            reverse("management_metric_import"),
            {
                "data_kind": "ACTUAL",
                "money_unit": "WAN",
                "report_code": "PL_TOTAL_NOWINE",
                "file": uploaded,
            },
        )
        self.assertEqual(response.status_code, 200)
        token = self.client.session["management_metric_preview"]
        self.assertContains(response, "指标测试酒店")
        self.assertEqual(preview_import.call_args.kwargs["money_unit"], "WAN")
        response = self.client.post(
            reverse("management_metric_confirm"),
            {"token": token, "reason": "导入经审核历史底稿", "expected_latest_id": "0"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(confirm_import.call_args.kwargs["expected_latest_id"], 0)
        self.assertEqual(confirm_import.call_args.kwargs["reason"], "导入经审核历史底稿")

    @patch("budgeting.management_metric_views.service.comparison_data")
    def test_csv_export_quotes_formula_like_project_names(self, comparison):
        comparison.return_value = {
            "rows": [
                {"name": "=危险名称", "values": [1, 2, 3, 4], "delta": 1, "growth": 33.3}
            ],
            "totals": {},
            "periods": ["2024 实际", "2025 实际", "2026 预测", "2027 预算"],
            "metric_label": "收入合计",
            "unit": "MONEY",
            "trend": [],
        }
        self.client.force_login(self.admin)
        response = self.client.get(reverse("management_metric_export"), {"year": 2027, "metric": "revenue_total"})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8-sig")
        self.assertIn("'=危险名称", body)
        self.assertIn("指标", body)
        self.assertIn("金额单位", body)
        self.assertIn("报表口径", body)
        self.assertIn("比较基期", body)

    @patch("budgeting.management_metric_views.service.comparison_data")
    def test_ratio_csv_has_only_delta_column(self, comparison):
        comparison.return_value = {
            "rows": [{"name": "酒店A", "values": [0.5, 0.6, 0.7, 0.8], "delta": 0.1, "growth": None}],
            "totals": {},
            "periods": ["2024 实际", "2025 实际", "2026 预测", "2027 预算"],
            "metric_label": "出租率",
            "unit": "RATIO",
            "trend": [],
            "comparison_label": "2026 预测",
        }
        self.client.force_login(self.admin)
        response = self.client.get(reverse("management_metric_export"), {"year": 2027, "metric": "occ"})
        body = response.content.decode("utf-8-sig")
        self.assertIn("百分比", body)
        self.assertIn("变化（百分点）", body)
        self.assertNotIn("增长百分比", body)
