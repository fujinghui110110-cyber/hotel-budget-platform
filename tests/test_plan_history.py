from io import StringIO
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase
from budgeting.models import (BudgetCycle, FreezeSnapshot, HistoryBaselineValue, Project,
                              ProjectCycle, TemplateVersion, UploadVersion, User)
from budgeting.services.plan_history import (assert_current_history, bind_existing_history,
    confirm_history, current_binding, ensure_plan, validate_history_values)
from budgeting.services.budget_versions import create_and_open_budget_version, budget_version_rows


class PlanHistoryTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="history-admin", role="ADMIN")
        self.project = Project.objects.create(code="HT1", name="Hotel")
        self.user = User.objects.create_user(username="hotel", project=self.project)
        self.template = TemplateVersion.objects.create(version="history-test", budget_year=2027,
            file_path="none", manifest_path="none", formula_manifest_hash="b" * 64)
        self.cycle = BudgetCycle.objects.create(name="2027", budget_year=2027, template=self.template)
        ProjectCycle.objects.create(cycle=self.cycle, project=self.project)
        self.plan = ensure_plan(self.cycle)
        self.values = [dict(report_code="PL_TOTAL_WINE", row_code="R001", data_year=year,
            data_kind=kind, period="YEAR", unit="MONEY", value_int=10000,
            ratio_num=None, ratio_den=None, source_sheet="损益", source_cell=f"A{year}")
            for year, kind in [(2024, "ACTUAL"), (2025, "ACTUAL"), (2026, "FORECAST")]]

    def confirm(self, **extra):
        args = dict(plan=self.plan, project=self.project, values=self.values, actor=self.admin,
                    reason="核对原始账表后确认", source_identity={"sha256": "a" * 64}, expected_revision=0)
        args.update(extra)
        return confirm_history(**args)

    def test_history_all_years_immutable_and_project_cannot_confirm(self):
        with self.assertRaises(PermissionDenied):
            self.confirm(actor=self.user)
        baseline = self.confirm()
        self.assertEqual(set(baseline.values.values_list("data_year", flat=True)), {2024, 2025, 2026})
        with self.assertRaises(ValidationError):
            baseline.reason = "overwrite"
            baseline.save()
        with self.assertRaises(ValidationError):
            baseline.values.update(value_int=0)
        with self.assertRaises(ValidationError):
            baseline.values.first().delete()
        binding = current_binding(self.plan, self.project)
        self.assertEqual(validate_history_values(binding, self.values), [])
        changed = [dict(v) for v in self.values]
        changed[0]["value_int"] += 1
        self.assertEqual(validate_history_values(binding, changed)[0]["code"], "HISTORY_LOCK_VIOLATION")
        self.assertEqual(len(validate_history_values(binding, [])), 3)

    def test_revision_retains_snapshot_and_marks_unapproved_stale(self):
        self.confirm()
        old = current_binding(self.plan, self.project)
        snapshot = FreezeSnapshot.objects.create(cycle=self.cycle, history_bindings={str(self.project.pk): old.pk})
        upload = UploadVersion.objects.create(project=self.project, cycle=self.cycle, template=self.template,
            original_path="original.xlsx", sha256="c" * 64, history_binding=old)
        approved = UploadVersion.objects.create(project=self.project, cycle=self.cycle, template=self.template,
            original_path="approved.xlsx", sha256="d" * 64, history_binding=old, status="APPROVED")
        self.values[0]["value_int"] = 12000
        self.confirm(expected_revision=1)
        upload.refresh_from_db(); approved.refresh_from_db(); snapshot.refresh_from_db()
        self.assertTrue(upload.history_stale)
        self.assertFalse(approved.history_stale)
        self.assertEqual(snapshot.history_bindings[str(self.project.pk)], old.pk)
        self.assertEqual(old.baseline.values.order_by("data_year").first().value_int, 10000)
        with self.assertRaises(ValidationError):
            assert_current_history(upload)

    def test_cross_year_change_requires_explicit_impact_confirmation(self):
        baseline = self.confirm()
        cycle28 = BudgetCycle.objects.create(name="2028", budget_year=2028, template=self.template)
        plan28 = ensure_plan(cycle28)
        bind_existing_history(plan=plan28, project=self.project, baseline=baseline, actor=self.admin, reason="复用已确认历史")
        with self.assertRaises(ValidationError):
            self.confirm(expected_revision=1)
        revised = self.confirm(expected_revision=1, affected_plan_ids=[self.plan.pk, plan28.pk])
        self.assertEqual(current_binding(plan28, self.project).baseline_id, revised.pk)

    def test_fixed_membership_survives_inactive_and_across_rounds(self):
        self.project.is_active = False; self.project.save()
        later = create_and_open_budget_version(budget_year=2027, actor=self.admin, template=self.template)
        self.assertEqual(later.plan_id, self.plan.pk)
        self.assertTrue(ProjectCycle.objects.filter(cycle=later, project=self.project).exists())
        self.assertEqual([row.project.pk for row in budget_version_rows(later)], [self.project.pk])

    def test_dry_run_is_repeatable_and_never_confirms(self):
        a, b = StringIO(), StringIO()
        call_command("migrate_annual_plans", stdout=a)
        call_command("migrate_annual_plans", stdout=b)
        self.assertEqual(a.getvalue(), b.getvalue())
        self.assertEqual(HistoryBaselineValue.objects.count(), 0)

    def test_workbook_history_reads_actual_cells_and_detects_tamper(self):
        import tempfile
        from pathlib import Path
        from openpyxl import Workbook
        from budgeting.models import ValidationRun
        from budgeting.services.history_workbook import template_cell_payload, validate_workbook_history
        self.confirm()
        upload = UploadVersion.objects.create(project=self.project, cycle=self.cycle, template=self.template,
            original_path="original.xlsx", sha256="e" * 64)
        run = ValidationRun.objects.create(upload=upload)
        manifest = {"reports": {"PL_TOTAL_WINE": {"sheet": "损益", "region": {"header_row": 1},
                     "mapping": [{"row_code": "R001", "cell": "D2", "unit": "MONEY"}]}}}
        wb = Workbook(); ws = wb.active; ws.title = "损益"
        ws.append(["2024年实际", "2025年实际", "2026年预测"])
        ws.append([100, 100, 100])
        payload = template_cell_payload(current_binding(self.plan, self.project), manifest, wb)
        self.assertEqual({v["cell"] for v in payload}, {"A2", "B2", "C2"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.xlsx"
            wb.save(path)
            self.assertEqual(validate_workbook_history(upload, path, run, manifest), 0)
            ws["B2"] = 101; wb.save(path)
            self.assertEqual(validate_workbook_history(upload, path, run, manifest), 1)
            self.assertTrue(run.issues.filter(code="HISTORY_LOCK_VIOLATION", location="损益!B2").exists())
        wb.close()

    def test_no_append_to_locked_values_or_silent_scope_loss(self):
        baseline = self.confirm()
        original = baseline.values.first()
        with self.assertRaises(ValidationError):
            HistoryBaselineValue.objects.bulk_create([HistoryBaselineValue(
                baseline=baseline, report_code="PL_TOTAL_WINE", row_code="EXTRA", data_year=2024,
                data_kind="ACTUAL", period="YEAR", unit="MONEY", value_int=10)])
        with self.assertRaises(ValidationError):
            HistoryBaselineValue.objects.create(baseline=baseline, report_code="PL_TOTAL_WINE",
                row_code="EXTRA", data_year=2024, data_kind="ACTUAL", period="YEAR", unit="MONEY", value_int=10)
        with self.assertRaises(ValidationError):
            self.confirm(expected_revision=1, values=self.values[:1])
        self.assertEqual(baseline.values.count(), 3)

    def test_migration_inventory_preserves_conflicting_history_for_confirmation(self):
        import json
        from budgeting.models import HistoricalImport, HistoricalValue, HistoryBaseline, PlanHistoryBinding
        sources = []
        for report, amount, active in [('PL_TOTAL_WINE', 10001, False), ('PL_TOTAL_WINE', 20002, True), ('PL_TOTAL_NOWINE', 30003, True)]:
            source = HistoricalImport.objects.create(project=self.project, data_year=2025,
                data_kind='ACTUAL', report_code=report, original_name='history.xlsx',
                original_path='isolated/history.xlsx', sha256=str(amount).ljust(64, '0'),
                active=active, created_by=self.admin)
            HistoricalValue.objects.create(import_batch=source, row_code='R001', row_label='收入',
                period='A2025', unit='MONEY', value_int=amount, source_sheet='损益', source_cell='B2')
            sources.append(source)
        before = list(HistoricalValue.objects.order_by('pk').values_list('pk', 'value_int'))
        output = StringIO()
        call_command('migrate_annual_plans', '--dry-run', stdout=output)
        result = json.loads(output.getvalue())
        self.assertEqual(list(result['possible_conflicts']), [f'{self.project.pk}:2025:ACTUAL:PL_TOTAL_WINE'])
        self.assertEqual(set(next(iter(result['possible_conflicts'].values()))), {str(s.pk) for s in sources[:2]})
        self.assertTrue(all(c['migration_state'] == 'PENDING_ADMIN_CONFIRMATION' for c in result['history_candidates']))
        self.assertEqual(HistoricalImport.objects.filter(active=True).count(), 2)
        self.assertEqual(list(HistoricalValue.objects.order_by('pk').values_list('pk', 'value_int')), before)
        self.assertEqual(HistoryBaseline.objects.count(), 0)
        self.assertEqual(PlanHistoryBinding.objects.count(), 0)
