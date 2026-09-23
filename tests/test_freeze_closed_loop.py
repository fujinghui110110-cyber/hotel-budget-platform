import json
from decimal import Decimal
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings

from budgeting.models import (
    BudgetCycle,
    FreezeSnapshot,
    NormalizedValue,
    PlanHistoryBinding,
    PlanProject,
    Project,
    ProjectCycle,
    SnapshotArtifact,
    TemplateVersion,
    UploadVersion,
    User,
)
from budgeting.services.files import sha256_file
from budgeting.services.plan_history import confirm_history, current_binding, ensure_plan
from budgeting.services.report_context import ReportContext
from budgeting.services.report_presenter import render_drilldown
from budgeting.services.report_query import query_report
from budgeting.services.workflow import freeze_cycle


class FreezeClosedLoopTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = self.root / "storage"
        self.storage.mkdir()
        self.settings = override_settings(BASE_DIR=self.root, BUDGET_STORAGE_ROOT=self.storage)
        self.settings.enable()
        self.addCleanup(self.settings.disable)

        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "template_version": "freeze-test",
                    "reports": {
                        "PL_TOTAL_WINE": {
                            "mapping": [
                                {
                                    "row_code": "R001",
                                    "row_label": "收入",
                                    "unit": "MONEY",
                                    "aggregation": "SUM",
                                    "cell": "A1",
                                },
                                {
                                    "row_code": "ROOMS",
                                    "row_label": "房晚",
                                    "unit": "COUNT",
                                    "aggregation": "SUM",
                                    "cell": "A2",
                                },
                                {
                                    "row_code": "REVENUE",
                                    "row_label": "收入",
                                    "unit": "MONEY",
                                    "aggregation": "SUM",
                                    "cell": "A3",
                                },
                                {
                                    "row_code": "ADR",
                                    "row_label": "ADR",
                                    "unit": "MONEY",
                                    "aggregation": "DERIVED",
                                    "cell": "A4",
                                    "numerator_cell": "A3",
                                    "denominator_cell": "A2",
                                },
                            ]
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.template = TemplateVersion.objects.create(
            version="freeze-test",
            budget_year=2027,
            file_path="template.xlsx",
            manifest_path=str(manifest),
            formula_manifest_hash="f" * 64,
        )
        self.project = Project.objects.create(code="HT1", name="Hotel 1")
        self.cycle = BudgetCycle.objects.create(name="2027 R1", budget_year=2027, template=self.template)
        self.plan = ensure_plan(self.cycle)
        PlanProject.objects.get_or_create(plan=self.plan, project=self.project)
        self.project_cycle = ProjectCycle.objects.create(cycle=self.cycle, project=self.project)
        self.upload = self._approved_upload("original-A.xlsx", b"original-A", 10000)
        self.project_cycle.current_upload = self.upload
        self.project_cycle.save(update_fields=["current_upload"])

    def _approved_upload(self, name, content, value):
        rel = Path("uploads") / name
        path = self.storage / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        upload = UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            template=self.template,
            status=UploadVersion.Status.APPROVED,
            original_name=name,
            original_path=str(rel),
            sha256=sha256_file(path),
        )
        NormalizedValue.objects.create(
            upload=upload,
            report_code="PL_TOTAL_WINE",
            row_code="R001",
            row_label="收入",
            period="YEAR",
            data_year=2027,
            data_kind="BUDGET",
            unit=NormalizedValue.Unit.MONEY,
            value_int=value,
            source_sheet="损益",
            source_cell="A1",
        )
        return upload


    def _add_metric(self, upload, row_code, unit, value=0, num=None, den=None, period="01"):
        return NormalizedValue.objects.create(
            upload=upload,
            report_code="PL_TOTAL_WINE",
            row_code=row_code,
            row_label=row_code,
            period=period,
            month=int(period) if period.isdigit() else None,
            data_year=2027,
            data_kind="BUDGET",
            unit=unit,
            value_int=value,
            ratio_num=num,
            ratio_den=den,
            source_sheet="损益",
            source_cell="A1",
        )

    def _approved_upload_for_project(self, project, name, content):
        rel = Path("uploads") / project.code / name
        path = self.storage / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return UploadVersion.objects.create(
            project=project,
            cycle=self.cycle,
            template=self.template,
            status=UploadVersion.Status.APPROVED,
            original_name=name,
            original_path=str(rel),
            sha256=sha256_file(path),
        )

    def _confirm_history(self, value=10000, expected_revision=0):
        admin = User.objects.create_user(username=f"admin-{expected_revision}", role="ADMIN")
        values = [
            {
                "report_code": "PL_TOTAL_WINE",
                "row_code": "R001",
                "data_year": year,
                "data_kind": kind,
                "period": "YEAR",
                "unit": "MONEY",
                "value_int": value,
                "ratio_num": None,
                "ratio_den": None,
                "source_sheet": "损益",
                "source_cell": f"A{year}",
            }
            for year, kind in [(2024, "ACTUAL"), (2025, "ACTUAL"), (2026, "FORECAST")]
        ]
        confirm_history(
            plan=self.plan,
            project=self.project,
            values=values,
            actor=admin,
            reason="核对原始账表后确认",
            source_identity={"sha256": "a" * 64, "value": value},
            expected_revision=expected_revision,
        )
        return current_binding(self.plan, self.project)

    def test_replacing_approved_upload_during_freeze_aborts_snapshot_publish(self):
        from budgeting.services import workflow as workflow_module

        real_freeze_selection_payload = workflow_module.freeze_selection_payload
        calls = {"count": 0}

        def racing_selection(context):
            payload = real_freeze_selection_payload(context)
            calls["count"] += 1
            if calls["count"] == 1:
                self._approved_upload("replacement.xlsx", b"replacement", 20000)
            return payload

        with patch("budgeting.services.workflow.freeze_selection_payload", side_effect=racing_selection):
            with self.assertRaisesRegex(ValueError, "冻结提交前版本状态发生变化|固定选择不一致"):
                freeze_cycle(self.cycle)

        self.cycle.refresh_from_db()
        self.assertNotEqual(self.cycle.status, BudgetCycle.Status.FROZEN)
        self.assertFalse(FreezeSnapshot.objects.filter(cycle=self.cycle, status=FreezeSnapshot.Status.COMPLETE).exists())

    def test_new_history_binding_does_not_change_completed_snapshot(self):
        old_binding = self._confirm_history(value=10000, expected_revision=0)
        self.upload.history_binding = old_binding
        self.upload.history_stale = False
        self.upload.save(update_fields=["history_binding", "history_stale"])

        snapshot = freeze_cycle(self.cycle)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, FreezeSnapshot.Status.COMPLETE)
        self.assertEqual(snapshot.history_bindings[str(self.project.pk)]["binding_id"], old_binding.pk)

        self._confirm_history(value=12000, expected_revision=1)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.history_bindings[str(self.project.pk)]["binding_id"], old_binding.pk)
        self.assertNotEqual(current_binding(self.plan, self.project).pk, old_binding.pk)

        artifact = SnapshotArtifact.objects.get(
            snapshot=snapshot,
            relative_path=f"{snapshot.directory}/report-selection.json",
        )
        selection = json.loads((self.storage / artifact.relative_path).read_text(encoding="utf-8"))
        self.assertEqual(selection["history_bindings"][str(self.project.pk)]["binding_id"], old_binding.pk)

    def test_main_drilldown_and_frozen_export_share_query_report_weighted_semantics(self):
        second_project = Project.objects.create(code="HT2", name="Hotel 2")
        PlanProject.objects.create(plan=self.plan, project=second_project)
        first = self.upload
        second = self._approved_upload_for_project(second_project, "original-B.xlsx", b"original-B")
        ProjectCycle.objects.create(cycle=self.cycle, project=second_project, current_upload=second)

        self._add_metric(first, "ROOMS", NormalizedValue.Unit.COUNT, value=10)
        self._add_metric(first, "REVENUE", NormalizedValue.Unit.MONEY, value=80000)
        self._add_metric(first, "ADR", NormalizedValue.Unit.MONEY, num=80000, den=10)
        self._add_metric(second, "ROOMS", NormalizedValue.Unit.COUNT, value=40)
        self._add_metric(second, "REVENUE", NormalizedValue.Unit.MONEY, value=400000)
        self._add_metric(second, "ADR", NormalizedValue.Unit.MONEY, num=400000, den=40)

        context = ReportContext(
            2027,
            self.cycle.pk,
            "PL_TOTAL_WINE",
            "APPROVED",
            (self.project.pk, second_project.pk),
            period="01",
        )
        main_result = query_report(context)
        adr = next(metric for metric in main_result.metrics if metric.row_code == "ADR")
        self.assertEqual(adr.value, Decimal("96.00"))
        self.assertEqual(set(adr.by_project.values()), {Decimal("80"), Decimal("100")})

        request = RequestFactory().get("/", {
            "row_code": "ADR",
            "period": "01",
            "source_mode": "APPROVED",
        })
        request.user = User.objects.create_user(username="drill-admin", role="ADMIN")
        response = render_drilldown(request, self.cycle, "PL_TOTAL_WINE")
        self.assertContains(response, "96.00")
        self.assertContains(response, "80.00")
        self.assertContains(response, "100.00")

        snapshot = freeze_cycle(self.cycle)
        frozen_report = json.loads((self.storage / snapshot.directory / "四表汇总.json").read_text(encoding="utf-8"))
        frozen_adr = frozen_report["PL_TOTAL_WINE"]["ADR|01"]
        self.assertEqual(frozen_adr["value"], "96.00")
        self.assertEqual(frozen_adr["value_int"], 9600)
        self.assertEqual(set(frozen_adr["by_project"].values()), {"80", "100"})
        self.assertEqual(frozen_adr["missing"], {})
