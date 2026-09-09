import json
import tempfile
from pathlib import Path

from django.test import TestCase
from openpyxl import Workbook

from budgeting.excel.extract import assign_dimensions, extract_report_values
from budgeting.models import BudgetCycle, NormalizedValue, Project, TemplateVersion, UploadVersion, ValidationRun


class ExtractionV2Tests(TestCase):
    def test_missing_recalculated_amount_is_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workbook = Workbook()
            workbook.active.title = "酒店损益总表（含名酒）"
            workbook.active["L32"] = "=1+1"
            workbook.save(root / "recalc.xlsx")
            manifest = {"management_v2": True, "reports": {"PL_TOTAL_WINE": {
                "sheet": workbook.active.title,
                "mapping": [{"row_code": "R0032", "period": "01", "cell": "L32", "unit": "MONEY"}],
            }}}
            (root / "manifest.json").write_text(json.dumps(manifest))
            template = TemplateVersion.objects.create(version="MISSING", budget_year=2027, manifest_path=str(root / "manifest.json"))
            cycle = BudgetCycle.objects.create(name="2027", budget_year=2027)
            project = Project.objects.create(code="MISSING", name="缺数验证")
            upload = UploadVersion.objects.create(project=project, cycle=cycle, template=template)
            run = ValidationRun.objects.create(upload=upload, rule_version="v2")
            self.assertEqual(extract_report_values(upload, root / "recalc.xlsx", run), 0)
            self.assertTrue(run.issues.filter(code="REPORT_VALUE_MISSING", severity="P0").exists())
            self.assertFalse(NormalizedValue.objects.filter(upload=upload).exists())
            manifest["reports"]["PL_TOTAL_WINE"]["mapping"][0]["unit"] = "COUNT"
            (root / "manifest.json").write_text(json.dumps(manifest))
            workbook.active["L32"] = 1.5
            workbook.save(root / "recalc.xlsx")
            self.assertEqual(extract_report_values(upload, root / "recalc.xlsx", run), 0)
            self.assertTrue(run.issues.filter(code="REPORT_VALUE_INVALID").exists())
            for invalid in ["非数值", True, "NaN", "Infinity"]:
                with self.subTest(invalid=invalid):
                    workbook.active["L32"] = invalid
                    workbook.save(root / "recalc.xlsx")
                    run.issues.all().delete()
                    self.assertEqual(extract_report_values(upload, root / "recalc.xlsx", run), 0)
                    self.assertTrue(run.issues.filter(code="REPORT_VALUE_INVALID").exists())
                    self.assertFalse(NormalizedValue.objects.filter(upload=upload).exists())

    def test_dimensions_roll_with_cycle_without_inventing_months(self):
        for period, expected in [("01", (2027, "BUDGET", 1)), ("YEAR", (2027, "BUDGET", None)), ("A2024", (2024, "ACTUAL", None)), ("F2026M12", (2026, "FORECAST", 12))]:
            value = NormalizedValue(period=period)
            assign_dimensions(value, 2027)
            self.assertEqual((value.data_year, value.data_kind, value.month), expected)

    def test_v3_history_layout_is_the_only_source_of_history_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "酒店损益总表（含名酒）"
            sheet["L32"] = 1
            sheet["Y1"] = "2025年实际"
            sheet["Y32"] = 9
            workbook.save(root / "recalc.xlsx")
            manifest = {
                "management_v2": True,
                "management_v3": True,
                "history_layout": {"sheet": "历史月度输入", "columns": []},
                "reports": {
                    "PL_TOTAL_WINE": {
                        "sheet": sheet.title,
                        "region": {"header_row": 1},
                        "mapping": [{"row_code": "R0032", "period": "01", "cell": "L32", "unit": "MONEY"}],
                    }
                },
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            template = TemplateVersion.objects.create(version="V3-HISTORY", budget_year=2027, manifest_path=str(manifest_path))
            cycle = BudgetCycle.objects.create(name="2027", budget_year=2027)
            project = Project.objects.create(code="V3-HISTORY", name="历史口径验证")
            upload = UploadVersion.objects.create(project=project, cycle=cycle, template=template)

            self.assertEqual(extract_report_values(upload, root / "recalc.xlsx"), 1)
            self.assertTrue(NormalizedValue.objects.filter(upload=upload, period="01").exists())
            self.assertFalse(NormalizedValue.objects.filter(upload=upload, period="A2025").exists())

            manifest["management_v3"] = False
            manifest.pop("history_layout")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(extract_report_values(upload, root / "recalc.xlsx"), 2)
            self.assertTrue(NormalizedValue.objects.filter(upload=upload, period="A2025").exists())

    def test_v3_fixture_does_not_import_legacy_four_table_history_values(self):
        path = Path("storage/uploads/DEMO01/39876717-3d4b-41bb-b2a7-e81c518f182e/original.recalculated.xlsx")
        if not path.exists():
            self.skipTest("V3 boundary fixture is not available")
        template = TemplateVersion.objects.create(
            version="V3-FIXTURE",
            budget_year=2027,
            manifest_path="artifacts/v3/2027/template_manifest_V3_2027.json",
        )
        cycle = BudgetCycle.objects.create(name="2027 fixture", budget_year=2027)
        project = Project.objects.create(code="V3-FIXTURE", name="V3真实边界")
        upload = UploadVersion.objects.create(project=project, cycle=cycle, template=template)

        self.assertGreater(extract_report_values(upload, path), 0)
        for row_code, period in (("R0079", "F2026"), ("R0044", "A2025"), ("R0056", "A2025")):
            self.assertFalse(
                NormalizedValue.objects.filter(upload=upload, row_code=row_code, period=period).exists(),
                f"legacy four-table history value leaked: {row_code} {period}",
            )
