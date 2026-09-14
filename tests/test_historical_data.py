import tempfile
from pathlib import Path
from django.test import TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch

from budgeting.models import (BudgetCycle, HistoricalImport, HistoricalValue, NormalizedValue,
                              Project, ProjectCycle, UploadVersion, User)
from budgeting.services.historical_data import confirm_history, sync_history
from budgeting.services.trends import aggregate_metric


class HistoricalDataTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(code="HX", name="海星酒店")
        self.other = Project.objects.create(code="HY", name="海月酒店")
        self.admin = User.objects.create_user(username="manager", role="ADMIN", password="test-pass-long")
        self.cycle = BudgetCycle.objects.create(name="预算", budget_year=2027, status="OPEN")
        self.upload = UploadVersion.objects.create(project=self.project, cycle=self.cycle, status="VALIDATED", original_path="test.xlsx", sha256="0"*64)
        self.proposal = {
            "canonical": [{"row_code": "R0032", "row_label": "酒店总收入", "unit": "MONEY"}],
            "rows": [{"source_key": "损益!12", "source_label": "营业收入合计", "values": [
                {"month": None, "unit": "MONEY", "value_int": 120000, "ratio_num": None, "ratio_den": None,
                 "source_sheet": "损益", "source_cell": "E12", "source_formula": ""}
            ]}],
        }

    def batch(self, project=None):
        return HistoricalImport.objects.create(project=project or self.project, data_year=2026, data_kind="FORECAST",
            report_code="PL_TOTAL_WINE", original_name="海星酒店-损益.xlsx", original_path="history.xlsx", sha256="a"*64,
            proposal=self.proposal, created_by=self.admin)

    def test_confirm_populates_all_read_surfaces_and_future_upload_without_changing_budget(self):
        NormalizedValue.objects.create(upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0032", period="YEAR",
                                      data_year=2027, data_kind="BUDGET", unit="MONEY", value_int=230000)
        batch = self.batch()
        confirm_history(batch, {"损益!12": "R0032"}, self.admin)
        result = aggregate_metric(self.cycle, "revenue_total", year=2026, kind="FORECAST", data_scope="latest")
        self.assertEqual(result["value_int"], 120000)
        self.assertEqual(NormalizedValue.objects.get(upload=self.upload, period="YEAR").value_int, 230000)
        newer = UploadVersion.objects.create(project=self.project, cycle=self.cycle, status="VALIDATED", original_path="new.xlsx", sha256="b"*64)
        sync_history(newer)
        self.assertEqual(NormalizedValue.objects.get(upload=newer).history_import_id, batch.pk)
        self.assertEqual(HistoricalValue.objects.get(import_batch=batch).value_int, 120000)

    def test_replacement_and_project_isolation_and_frozen_immutability(self):
        old = self.batch()
        confirm_history(old, {"损益!12": "R0032"}, self.admin)
        frozen = BudgetCycle.objects.create(name="冻结", budget_year=2028, status="FROZEN")
        locked = UploadVersion.objects.create(project=self.project, cycle=frozen, status="APPROVED", original_path="frozen.xlsx", sha256="f"*64)
        NormalizedValue.objects.create(upload=locked, report_code="PL_TOTAL_WINE", row_code="R0032", period="F2026",
                                      data_year=2026, data_kind="FORECAST", unit="MONEY", value_int=500)
        unrelated = UploadVersion.objects.create(project=self.other, cycle=self.cycle, original_path="other.xlsx", sha256="c"*64)
        new = self.batch()
        new.proposal["rows"][0]["values"][0]["value_int"] = 999
        new.save()
        confirm_history(new, {"损益!12": "R0032"}, self.admin)
        old.refresh_from_db()
        self.assertFalse(old.active)
        self.assertEqual(NormalizedValue.objects.get(upload=self.upload).value_int, 999)
        self.assertEqual(NormalizedValue.objects.get(upload=locked).value_int, 500)
        self.assertFalse(NormalizedValue.objects.filter(upload=unrelated).exists())
        self.assertEqual(HistoricalValue.objects.get(import_batch=old).value_int, 120000)

    def test_unconfirmed_and_invalid_mapping_never_changes_data(self):
        batch = self.batch()
        self.assertFalse(NormalizedValue.objects.filter(upload=self.upload).exists())
        with self.assertRaisesMessage(ValueError, "无效"):
            confirm_history(batch, {"损益!12": "invented"}, self.admin)
        self.assertFalse(batch.values.exists())
        self.assertFalse(HistoricalImport.objects.filter(active=True).exists())

    def test_duplicate_mappings_and_repeat_confirmation_rejected(self):
        batch = self.batch()
        batch.proposal["rows"].append({**batch.proposal["rows"][0], "source_key": "损益!13"})
        batch.save()
        with self.assertRaisesMessage(ValueError, "同一"):
            confirm_history(batch, {"损益!12": "R0032", "损益!13": "R0032"}, self.admin)
        confirm_history(batch, {"损益!12": "R0032"}, self.admin)
        with self.assertRaisesMessage(ValueError, "已经确认"):
            confirm_history(batch, {"损益!12": "R0032"}, self.admin)

    def test_project_user_cannot_upload_review_confirm_or_download(self):
        user = User.objects.create_user(username="project", role="PROJECT", project=self.project)
        self.client.force_login(user)
        batch = self.batch()
        for path in ["/management/history/", f"/management/history/{batch.pk}/", f"/management/history/{batch.pk}/original/"]:
            self.assertIn(self.client.get(path).status_code, (403, 404))
            self.assertIn(self.client.post(path, {"mapping_0": "R0032"}).status_code, (403, 404))
        with self.assertRaises(ValueError):
            confirm_history(batch, {"损益!12": "R0032"}, user)

    def test_unconfirmed_budget_history_is_removed_but_budget_is_retained(self):
        for kind, year, period in [("ACTUAL", 2025, "A2025"), ("FORECAST", 2026, "F2026"), ("BUDGET", 2027, "YEAR")]:
            NormalizedValue.objects.create(upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0032",
                                          period=period, data_year=year, data_kind=kind, unit="MONEY", value_int=123)
        sync_history(self.upload)
        self.assertEqual(list(NormalizedValue.objects.filter(upload=self.upload).values_list("data_kind", flat=True)), ["BUDGET"])

    def test_upload_form_wan_unit_matches_parser(self):
        from decimal import Decimal
        from budgeting.history_views import HistoryUploadForm
        from budgeting.services.history_parser import _convert_number
        self.assertIn(("wan", "万元"), list(HistoryUploadForm().fields["money_unit"].choices))
        self.assertEqual(_convert_number(Decimal("1"), "MONEY", "wan")["value_int"], 1000000)
        self.assertEqual(_convert_number(Decimal("1"), "COUNT", "wan")["value_int"], 1)

    @patch("budgeting.excel.history._load_manifest", return_value={"management_v2": True})
    @patch("budgeting.excel.history._sync_dimensions")
    @patch("budgeting.excel.history._sync_metric_metadata")
    @patch("budgeting.excel.history.load_workbook")
    def test_budget_pipeline_does_not_read_or_validate_history_sheet(self, load, metric_sync, dim_sync, manifest):
        from budgeting.excel.history import extract_management_values
        extract_management_values(self.upload, "/nonexistent.xlsx", include_history=False)
        load.assert_not_called()
        dim_sync.assert_called_once()

    def test_migration_only_discards_unconfirmed_history_of_unfrozen_cycles(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        migration = importlib.import_module("budgeting.migrations.0011_independent_history_cache")
        frozen_cycle = BudgetCycle.objects.create(name="历史冻结", budget_year=2028, status="FROZEN")
        frozen_upload = UploadVersion.objects.create(project=self.project, cycle=frozen_cycle, original_path="frozen.xlsx", sha256="f"*64)
        for upload in (self.upload, frozen_upload):
            for kind, year, period in [("ACTUAL", 2025, "A2025"), ("BUDGET", 2027, "YEAR")]:
                NormalizedValue.objects.create(upload=upload, report_code="PL_TOTAL_WINE", row_code="R0032", period=period,
                                              data_year=year, data_kind=kind, unit="MONEY", value_int=10)
        migration.discard_budget_history(apps, None)
        self.assertEqual(NormalizedValue.objects.filter(upload=self.upload).count(), 1)
        self.assertEqual(NormalizedValue.objects.filter(upload=frozen_upload).count(), 2)

    def test_admin_history_does_not_block_project_budget_validation(self):
        from budgeting.models import ValidationRun
        from budgeting.excel.business_checks import validate_management_values
        run = ValidationRun.objects.create(upload=self.upload, rule_version="test")
        for code, amount in [("R0032", 9999), ("R0041", 1), ("R0049", 2), ("R0057", 3), ("R0062", 4)]:
            NormalizedValue.objects.create(upload=self.upload, report_code="PL_TOTAL_WINE", row_code=code, period="A2025",
                                          data_year=2025, data_kind="ACTUAL", unit="MONEY", value_int=amount)
        validate_management_values(self.upload, run)
        self.assertFalse(run.issues.filter(severity="P0").exists())
