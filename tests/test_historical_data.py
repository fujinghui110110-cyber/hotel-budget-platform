import tempfile
from pathlib import Path
from django.test import TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch

from budgeting.models import (BudgetCycle, HistoricalImport, HistoricalValue, NormalizedValue,
                              Project, ProjectCycle, UploadVersion, User, HistoryBaseline, BudgetPlan, PlanProject, PlanHistoryBinding)
from budgeting.services.plan_history import ensure_plan, current_binding
from budgeting.services.historical_data import confirm_history, sync_history
from budgeting.services.trends import aggregate_metric


class HistoricalDataTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(code="HX", name="海星酒店")
        self.other = Project.objects.create(code="HY", name="海月酒店")
        self.admin = User.objects.create_user(username="manager", role="ADMIN", password="test-pass-long")
        self.cycle = BudgetCycle.objects.create(name="预算", budget_year=2027, status="OPEN")
        ProjectCycle.objects.create(cycle=self.cycle, project=self.project)
        self.plan = ensure_plan(self.cycle)
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

    def confirm(self, batch, selections, actor, **extra):
        previous = HistoryBaseline.objects.filter(project=batch.project).order_by('-revision').first()
        args = dict(plan=self.plan,reason='核对原件后确认',expected_revision=previous.revision if previous else 0)
        args.update(extra)
        return confirm_history(batch,selections,actor,**args)

    def test_confirm_preserves_old_upload_and_future_upload_pins_baseline(self):
        NormalizedValue.objects.create(upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0032", period="YEAR",
                                      data_year=2027, data_kind="BUDGET", unit="MONEY", value_int=230000)
        batch = self.batch()
        self.confirm(batch, {"损益!12": "R0032"}, self.admin)
        self.assertEqual(NormalizedValue.objects.get(upload=self.upload, period="YEAR").value_int, 230000)
        self.assertFalse(NormalizedValue.objects.filter(upload=self.upload,data_kind='FORECAST').exists())
        self.upload.refresh_from_db()
        self.assertTrue(self.upload.history_stale)
        binding = current_binding(self.plan,self.project)
        newer = UploadVersion.objects.create(project=self.project, cycle=self.cycle, history_binding=binding,
            status="VALIDATED", original_path="new.xlsx", sha256="b"*64)
        sync_history(newer)
        self.assertEqual(NormalizedValue.objects.get(upload=newer).value_int,120000)
        self.assertEqual(HistoricalValue.objects.get(import_batch=batch).value_int,120000)
        self.assertEqual(binding.baseline.values.get().value_int,120000)

    def test_replacement_and_project_isolation_and_frozen_immutability(self):
        old = self.batch()
        self.confirm(old, {"损益!12": "R0032"}, self.admin)
        old_binding = current_binding(self.plan,self.project)
        self.upload.history_binding = old_binding
        self.upload.history_stale = False
        self.upload.status = 'PROCESSING'
        self.upload.save()
        sync_history(self.upload)
        self.upload.status = 'APPROVED'
        self.upload.save()
        frozen = BudgetCycle.objects.create(name="冻结", budget_year=2028, status="FROZEN")
        locked = UploadVersion.objects.create(project=self.project, cycle=frozen, status="APPROVED", original_path="frozen.xlsx", sha256="f"*64)
        NormalizedValue.objects.create(upload=locked, report_code="PL_TOTAL_WINE", row_code="R0032", period="F2026",
                                      data_year=2026, data_kind="FORECAST", unit="MONEY", value_int=500)
        unrelated = UploadVersion.objects.create(project=self.other, cycle=self.cycle, original_path="other.xlsx", sha256="c"*64)
        new = self.batch()
        new.proposal["rows"][0]["values"][0]["value_int"] = 999
        new.save()
        self.confirm(new, {"损益!12": "R0032"}, self.admin)
        old.refresh_from_db()
        self.assertFalse(old.active)
        self.assertEqual(NormalizedValue.objects.get(upload=self.upload).value_int, 120000)
        self.assertEqual(current_binding(self.plan,self.project).baseline.values.get().value_int,999)
        self.assertEqual(old_binding.baseline.values.get().value_int,120000)
        self.assertEqual(NormalizedValue.objects.get(upload=locked).value_int, 500)
        self.assertFalse(NormalizedValue.objects.filter(upload=unrelated).exists())
        self.assertEqual(HistoricalValue.objects.get(import_batch=old).value_int, 120000)

    def test_unconfirmed_and_invalid_mapping_never_changes_data(self):
        batch = self.batch()
        self.assertFalse(NormalizedValue.objects.filter(upload=self.upload).exists())
        with self.assertRaisesMessage(ValueError, "无效"):
            self.confirm(batch, {"损益!12": "invented"}, self.admin)
        self.assertFalse(batch.values.exists())
        self.assertFalse(HistoricalImport.objects.filter(active=True).exists())

    def test_duplicate_mappings_and_repeat_confirmation_rejected(self):
        batch = self.batch()
        batch.proposal["rows"].append({**batch.proposal["rows"][0], "source_key": "损益!13"})
        batch.save()
        with self.assertRaisesMessage(ValueError, "同一"):
            self.confirm(batch, {"损益!12": "R0032", "损益!13": "R0032"}, self.admin)
        self.confirm(batch, {"损益!12": "R0032"}, self.admin)
        with self.assertRaisesMessage(ValueError, "已经确认"):
            self.confirm(batch, {"损益!12": "R0032"}, self.admin)

    def test_project_user_cannot_upload_review_confirm_or_download(self):
        user = User.objects.create_user(username="project", role="PROJECT", project=self.project)
        self.client.force_login(user)
        batch = self.batch()
        for path in ["/management/history/", f"/management/history/{batch.pk}/", f"/management/history/{batch.pk}/original/"]:
            self.assertIn(self.client.get(path).status_code, (403, 404))
            self.assertIn(self.client.post(path, {"mapping_0": "R0032"}).status_code, (403, 404))
        with self.assertRaises(ValueError):
            self.confirm(batch, {"损益!12": "R0032"}, user)

    def test_unbound_upload_history_and_budget_are_preserved(self):
        for kind, year, period in [("ACTUAL", 2025, "A2025"), ("FORECAST", 2026, "F2026"), ("BUDGET", 2027, "YEAR")]:
            NormalizedValue.objects.create(upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0032",
                                          period=period, data_year=year, data_kind=kind, unit="MONEY", value_int=123)
        sync_history(self.upload)
        self.assertEqual(set(NormalizedValue.objects.filter(upload=self.upload).values_list("data_kind", flat=True)), {"ACTUAL", "FORECAST", "BUDGET"})

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

    def test_admin_review_requires_explicit_reason_impact_and_creates_baseline(self):
        self.client.force_login(self.admin)
        batch = self.batch()
        url = f'/management/history/{batch.pk}/'
        response = self.client.get(url)
        self.assertContains(response,'历史版本与影响范围')
        data = dict(mapping_0='R0032',plan=self.plan.pk,expected_revision=0,reason='确认本次原始损益',confirm_impact='yes')
        rejected = dict(data,reason='')
        self.assertEqual(self.client.post(url,rejected).status_code,200)
        self.assertFalse(HistoryBaseline.objects.exists())
        self.assertEqual(self.client.post(url,data).status_code,302)
        baseline = HistoryBaseline.objects.get()
        self.assertEqual(baseline.reason,'确认本次原始损益')
        self.assertEqual(baseline.values.get().value_int,120000)
        self.assertEqual(PlanHistoryBinding.objects.get().baseline,baseline)

    def test_revision_merges_complete_range_and_rejects_stale_confirmation(self):
        from django.core.exceptions import ValidationError
        first = self.batch()
        first.data_year = 2025; first.data_kind = 'ACTUAL'; first.save()
        self.confirm(first,{'损益!12':'R0032'},self.admin)
        second = self.batch()
        self.confirm(second,{'损益!12':'R0032'},self.admin)
        latest = current_binding(self.plan,self.project).baseline
        self.assertEqual(set(latest.values.values_list('data_year',flat=True)),{2025,2026})
        third = self.batch()
        with self.assertRaises(ValidationError):
            self.confirm(third,{'损益!12':'R0032'},self.admin,expected_revision=1)
        self.assertFalse(third.values.exists())
        self.assertEqual(HistoryBaseline.objects.count(),2)

    def test_shared_year_impact_must_be_acknowledged(self):
        from budgeting.services.plan_history import bind_existing_history
        from django.core.exceptions import ValidationError
        first = self.batch(); self.confirm(first,{'损益!12':'R0032'},self.admin)
        other = BudgetPlan.objects.create(budget_year=2028)
        PlanProject.objects.create(plan=other,project=self.project)
        bind_existing_history(plan=other,project=self.project,baseline=HistoryBaseline.objects.get(),actor=self.admin,reason='采用同一已确认历史')
        second = self.batch()
        with self.assertRaises(ValidationError):
            self.confirm(second,{'损益!12':'R0032'},self.admin)
        self.confirm(second,{'损益!12':'R0032'},self.admin,affected_plan_ids=[self.plan.pk,other.pk])
        self.assertEqual(current_binding(other,self.project).baseline_id,current_binding(self.plan,self.project).baseline_id)

    def test_legacy_import_is_not_locked_without_explicit_reconfirmation(self):
        from django.utils import timezone
        legacy = self.batch()
        legacy.data_year = 2024; legacy.data_kind = 'ACTUAL'; legacy.active=True; legacy.confirmed_at=timezone.now(); legacy.save()
        HistoricalValue.objects.create(import_batch=legacy,row_code='R0032',row_label='收入',period='A2024',unit='MONEY',value_int=555,source_sheet='损益',source_cell='A1')
        fresh = self.batch(); self.confirm(fresh,{'损益!12':'R0032'},self.admin)
        self.assertFalse(current_binding(self.plan,self.project).baseline.values.filter(data_year=2024).exists())
        another = self.batch(); self.confirm(another,{'损益!12':'R0032'},self.admin,include_legacy=True,legacy_import_ids=[str(legacy.pk)])
        self.assertEqual(current_binding(self.plan,self.project).baseline.values.get(data_year=2024).value_int,555)
