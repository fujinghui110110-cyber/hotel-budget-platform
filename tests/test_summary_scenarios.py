import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from budgeting.excel.business_checks import TOTAL_RULES, ZZ_RULES
from budgeting.models import (
    AdjustmentBatch,
    BudgetCycle,
    NormalizedValue,
    Project,
    ProjectCycle,
    REPORTS,
    TemplateVersion,
    UploadVersion,
)
from budgeting.services.summary_scenarios import (
    _template_formula,
    build_summary_editor,
    calculate_summary_scenario,
    create_summary_scenario,
    issue_summary_scenario,
)
from budgeting.services.scenarios import ScenarioError
from budgeting.services.workflow import approve_upload
from tests.test_scenarios_v2 import _full_report_baseline


class SummaryScenarioTests(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        manifest_path = Path(self.tempdir.name) / "manifest.json"
        reports = {}
        for report_code in REPORTS:
            baseline = _full_report_baseline(report_code)
            zz = report_code.startswith("PL_ZZ")
            annual_col = "S" if zz else "J"
            month_col = "F" if zz else "L"
            mapping = []
            for code, row in baseline.items():
                number = int(code[1:])
                monthly_formula = ""
                annual_formula = f"SUM({month_col}{number})"
                aggregation = "SUM"
                if code == ("R0012" if zz else "R0029"):
                    sold = 11 if zz else 28
                    available = 10 if zz else 24
                    monthly_formula = f"IF({month_col}{available}=0,0,{month_col}{sold}/{month_col}{available})"
                    annual_formula = f"IF({annual_col}{available}=0,0,{annual_col}{sold}/{annual_col}{available})"
                    aggregation = "RATIO"
                elif code == ("R0013" if zz else "R0030"):
                    revenue = 21 if zz else 41
                    sold = 11 if zz else 28
                    monthly_formula = f"ROUND(IF({month_col}{sold}=0,0,{month_col}{revenue}/{month_col}{sold}),2)"
                    annual_formula = f"ROUND(IF({annual_col}{sold}=0,0,{annual_col}{revenue}/{annual_col}{sold}),2)"
                    aggregation = "DERIVED"
                elif code == ("R0014" if zz else "R0031"):
                    revenue = 21 if zz else 41
                    available = 10 if zz else 24
                    monthly_formula = f"ROUND(IF({month_col}{available}=0,0,{month_col}{revenue}/{month_col}{available}),2)"
                    annual_formula = f"ROUND(IF({annual_col}{available}=0,0,{annual_col}{revenue}/{annual_col}{available}),2)"
                    aggregation = "DERIVED"
                mapping.extend(
                    [
                        {
                            "row_code": code,
                            "row_label": code,
                            "cell": f"{month_col}{number}",
                            "source": {"cell": f"{month_col}{number}"},
                            "formula": monthly_formula,
                            "unit": row["unit"],
                            "aggregation": aggregation,
                        },
                        {
                            "row_code": code,
                            "row_label": code,
                            "cell": f"{annual_col}{number}",
                            "source": {"cell": f"{annual_col}{number}"},
                            "formula": annual_formula,
                            "unit": row["unit"],
                            "aggregation": aggregation,
                        },
                    ]
                )
            reports[report_code] = {"mapping": mapping}
        manifest_path.write_text(json.dumps({"reports": reports}), encoding="utf-8")

        User = get_user_model()
        self.admin = User.objects.create_user("summary-admin", password="x", role="ADMIN")
        self.project_user = User.objects.create_user("summary-project", password="x", role="PROJECT")
        self.project = Project.objects.create(code="SUM01", name="汇总测试项目")
        self.project_user.project = self.project
        self.project_user.save(update_fields=["project"])
        self.cycle = BudgetCycle.objects.create(
            name="2027预算R1", budget_year=2027, revision_no=1, status=BudgetCycle.Status.OPEN
        )
        self.template = TemplateVersion.objects.create(
            version="SUMMARY-2027",
            budget_year=2027,
            file_path="template.xlsx",
            manifest_path=str(manifest_path),
            formula_manifest_hash="a" * 64,
        )
        self.upload = UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            template=self.template,
            status=UploadVersion.Status.VALIDATED,
            original_name="项目预算.xlsx",
            original_path="uploads/summary/original.xlsx",
            sha256="b" * 64,
        )
        ProjectCycle.objects.create(project=self.project, cycle=self.cycle, is_open=True)
        self._seed_values()

    def _seed_values(self):
        for report_code in REPORTS:
            for code, row in _full_report_baseline(report_code).items():
                annual = row["before"]["YEAR"]
                unit = row["unit"]
                stored = annual["value_int"]
                if unit == "RATIO":
                    stored = round(annual["ratio_num"] / annual["ratio_den"] * 10_000)
                NormalizedValue.objects.create(
                    upload=self.upload,
                    report_code=report_code,
                    row_code=code,
                    row_label=code,
                    period="YEAR",
                    data_year=2027,
                    data_kind="BUDGET",
                    unit=unit,
                    value_int=stored,
                    ratio_num=annual.get("ratio_num"),
                    ratio_den=annual.get("ratio_den"),
                    source_sheet=report_code,
                    source_cell=f"J{int(code[1:])}",
                )
        for year, kind, value in ((2024, "ACTUAL", 700_000), (2025, "ACTUAL", 760_000), (2026, "FORECAST", 800_000)):
            NormalizedValue.objects.create(
                upload=self.upload,
                report_code="PL_TOTAL_WINE",
                row_code="R0041",
                row_label="客房收入",
                period=("A" if kind == "ACTUAL" else "F") + str(year),
                data_year=year,
                data_kind=kind,
                unit="MONEY",
                value_int=value,
                source_sheet="历史",
                source_cell="A1",
            )

    def test_editor_supports_four_reports_and_history_without_zero_filling(self):
        for report_code in REPORTS:
            rows = build_summary_editor(self.upload, report_code)
            self.assertTrue(rows)
            self.assertTrue(any(row["editable"] for row in rows))
            rule_targets = ZZ_RULES if report_code.startswith("PL_ZZ") else TOTAL_RULES
            indexed = {row["code"]: row for row in rows}
            for target in rule_targets:
                self.assertFalse(indexed[f"R{target:04d}"]["editable"])
                self.assertEqual(indexed[f"R{target:04d}"]["formula_kind"], "RECONCILIATION")
        room_revenue = next(row for row in build_summary_editor(self.upload, "PL_TOTAL_WINE") if row["code"] == "R0041")
        self.assertEqual([item["value_int"] for item in room_revenue["history"]], [700_000, 760_000, 800_000])
        missing_history = next(row for row in build_summary_editor(self.upload, "PL_TOTAL_WINE") if row["code"] == "R0042")
        self.assertEqual([item["value_int"] for item in missing_history["history"]], [None, None, None])

    def test_template_formula_prefers_non_simple_annual_bridge_only(self):
        bridge = {
            "monthly": "F113",
            "annual": "ROUND(S113+S121-SUM(S115:S120,S122)+S123,2)",
            "aggregation": "SUM",
            "row_num": 124,
        }
        simple = {
            "monthly": "F105*0.04",
            "annual": "ROUND(SUM(ROUND(F105,2),ROUND(G105,2),ROUND(H105,2),ROUND(I105,2),ROUND(J105,2),ROUND(K105,2),ROUND(L105,2),ROUND(M105,2),ROUND(N105,2),ROUND(O105,2),ROUND(P105,2),ROUND(Q105,2)),2)",
            "aggregation": "SUM",
            "row_num": 105,
        }

        self.assertEqual(_template_formula(bridge), bridge["annual"])
        self.assertEqual(_template_formula(simple), simple["monthly"])

    def test_annual_leaf_edit_recalculates_rules_and_issues_visible_targets(self):
        original_month_count = NormalizedValue.objects.filter(upload=self.upload, month__isnull=False).count()
        scenario = create_summary_scenario(
            self.upload,
            "客房收入年度调整",
            self.admin,
            "PL_TOTAL_WINE",
            reason="根据管理目标调整客房收入",
        )
        calculate_summary_scenario(scenario, {"R0041": "16800.00", "R0042": "130.00"}, self.admin)
        scenario.refresh_from_db()
        changed = {row["code"]: row for row in scenario.results["changed_rows"]}
        self.assertTrue(changed["R0041"]["entered"])
        self.assertTrue(changed["R0042"]["entered"])
        self.assertFalse(changed["R0032"]["entered"])
        self.assertEqual(changed["R0032"]["formula_kind"], "RECONCILIATION")
        self.assertEqual(changed["R0041"]["after"], 1_680_000)
        self.assertEqual(
            changed["R0032"]["after"],
            sum(changed.get(code, {"after": NormalizedValue.objects.get(upload=self.upload, report_code="PL_TOTAL_WINE", row_code=code, period="YEAR").value_int})["after"] for code in ("R0041", "R0049", "R0057", "R0062")),
        )
        self.assertEqual(NormalizedValue.objects.filter(upload=self.upload, month__isnull=False).count(), original_month_count)
        self.assertEqual(
            NormalizedValue.objects.get(upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0041", period="YEAR").value_int,
            840_000,
        )

        batch = issue_summary_scenario(scenario, self.admin)
        self.assertEqual(batch.status, AdjustmentBatch.Status.ISSUED)
        self.assertEqual(batch.cascade["kind"], "summary_annual")
        self.assertTrue(batch.cascade["requires_monthly_refill"])
        self.assertEqual(batch.cascade["source_identity"]["baseline_upload_id"], str(self.upload.pk))
        lines = {line.row_code: line for line in batch.lines.all()}
        self.assertEqual(lines["R0041"].target_cents, 1_680_000)
        self.assertEqual(lines["R0041"].period, "YEAR")
        self.assertIn("R0032", lines)
        self.assertEqual(batch.row_code, "R0041")
        self.assertEqual(batch.delta_cents, lines["R0041"].allocated_delta_cents)
        self.assertNotEqual(batch.delta_cents, sum(line.allocated_delta_cents for line in lines.values()))
        self.assertEqual(issue_summary_scenario(scenario, self.admin).pk, batch.pk)

    def test_formula_override_missing_source_and_stale_baseline_are_blocked(self):
        scenario = create_summary_scenario(
            self.upload, "公式保护", self.admin, "PL_TOTAL_WINE"
        )
        with self.assertRaisesMessage(ScenarioError, "公式联动项"):
            calculate_summary_scenario(scenario, {"R0032": "1.00"}, self.admin)

        NormalizedValue.objects.filter(
            upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0042", period="YEAR"
        ).delete()
        with self.assertRaisesMessage(ScenarioError, "不能按零计算"):
            calculate_summary_scenario(scenario, {"R0041": "16800.00"}, self.admin)

        newer = UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            template=self.template,
            status=UploadVersion.Status.SUBMITTED,
            original_name="新上传.xlsx",
            original_path="uploads/summary/new.xlsx",
            sha256="c" * 64,
        )
        with self.assertRaisesMessage(ScenarioError, "最新合格上传"):
            calculate_summary_scenario(scenario, {"R0041": "16800.00"}, self.admin)
        self.assertNotEqual(newer.pk, self.upload.pk)

    def test_unchanged_form_values_do_not_become_priority_changes(self):
        scenario = create_summary_scenario(
            self.upload, "无变化", self.admin, "PL_TOTAL_WINE"
        )
        editor = build_summary_editor(self.upload, "PL_TOTAL_WINE")
        overrides = {row["code"]: row["input_value"] for row in editor if row["editable"]}
        calculate_summary_scenario(scenario, overrides, self.admin)
        scenario.refresh_from_db()
        self.assertEqual(scenario.results["changed_rows"], [])
        with self.assertRaisesMessage(ScenarioError, "没有发生变化"):
            issue_summary_scenario(scenario, self.admin)

    def test_ratio_linkage_confirms_from_normalized_numerator_and_denominator(self):
        scenario = create_summary_scenario(
            self.upload, "出租率联动", self.admin, "PL_TOTAL_WINE"
        )
        calculate_summary_scenario(scenario, {"R0028": "9000"}, self.admin)
        batch = issue_summary_scenario(scenario, self.admin)
        ratio_line = batch.lines.get(row_code="R0029")
        self.assertEqual(ratio_line.target_cents, 7500)

        submitted = UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            template=self.template,
            status=UploadVersion.Status.SUBMITTED,
            original_name="落实调整.xlsx",
            original_path="uploads/summary/implemented.xlsx",
            sha256="d" * 64,
        )
        changes = {item["row_code"]: item for item in batch.cascade["changes"]}
        for line in batch.lines.all():
            change = changes[line.row_code]
            fields = {
                "value_int": line.target_cents,
                "ratio_num": None,
                "ratio_den": None,
            }
            if change["unit"] == "RATIO":
                fields = {"value_int": 0, "ratio_num": 3, "ratio_den": 4}
            NormalizedValue.objects.create(
                upload=submitted,
                report_code=line.report_code,
                row_code=line.row_code,
                row_label=change["label"],
                period="YEAR",
                data_year=2027,
                data_kind="BUDGET",
                unit=change["unit"],
                source_sheet=line.report_code,
                source_cell="J1",
                **fields,
            )
        approve_upload(submitted, self.admin)
        ratio_line.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(ratio_line.latest_value_cents, 7500)
        self.assertEqual(ratio_line.difference_cents, 0)
        self.assertEqual(ratio_line.status, ratio_line.Status.CONFIRMED)
        self.assertEqual(batch.status, AdjustmentBatch.Status.COMPLETED)

    def test_closed_old_cycle_cannot_issue_after_new_version_opens(self):
        scenario = create_summary_scenario(
            self.upload, "旧版本调整", self.admin, "PL_TOTAL_WINE"
        )
        calculate_summary_scenario(scenario, {"R0041": "16800.00"}, self.admin)
        ProjectCycle.objects.filter(project=self.project, cycle=self.cycle).update(is_open=False)
        newer_cycle = BudgetCycle.objects.create(
            name="2027预算R2", budget_year=2027, revision_no=2, status=BudgetCycle.Status.OPEN
        )
        ProjectCycle.objects.create(project=self.project, cycle=newer_cycle, is_open=True)
        with self.assertRaisesMessage(ScenarioError, "已不在此预算版本开放填报"):
            issue_summary_scenario(scenario, self.admin)
        scenario.refresh_from_db()
        self.assertEqual(scenario.status, "READY")
        self.assertIsNone(scenario.batch_id)
