import unittest

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from budgeting.models import (
    AdjustmentBatch,
    AdjustmentLine,
    BudgetCycle,
    NormalizedValue,
    Project,
    ProjectCycle,
    TemplateVersion,
    UploadVersion,
)
from budgeting.services.pnl_graph import (
    MONTHS,
    build_report_graph,
    parse_formula,
    recompute,
)
from budgeting.services.workflow import (
    create_full_adjustment,
    issue_adjustment,
    update_adjustment_lines_for_upload,
)

REPORT = "PL_TEST"


def _row(code, label, unit, agg, rnum, monthly="", annual=None):
    """Build the L/J mapping entries for one report row."""
    entries = [{
        "row_code": code, "row_label": label, "unit": unit, "aggregation": agg,
        "formula": monthly, "source": {"cell": f"L{rnum}"},
    }]
    if annual is not None:
        entries.append({
            "row_code": code, "row_label": label, "unit": unit, "aggregation": agg,
            "formula": annual, "source": {"cell": f"J{rnum}"},
        })
    return entries


def _manifest():
    mapping = []
    for code, label, unit, agg, rnum, monthly, annual in [
        ("R0041", "客房收入", "MONEY", "SUM", 41, "", ""),
        ("R0049", "餐饮收入", "MONEY", "SUM", 49, "", ""),
        ("R0080", "经营成本", "MONEY", "SUM", 80, "", ""),
        ("R0106", "财务费用", "MONEY", "SUM", 106, "'外部表'!K22", ""),
        ("R0024", "总可卖房", "COUNT", "SUM", 24, "", ""),
        ("R0028", "已售房", "COUNT", "SUM", 28, "", ""),
        ("R0029", "出租率", "RATIO", "RATIO", 29, "IF(L24=0,0,L28/L24)", "IF(J24=0,0,J28/J24)"),
        ("R0032", "酒店总收入", "MONEY", "SUM", 32, "L41+L49", ""),
        ("R0033", "经营毛利润", "MONEY", "SUM", 33, "L32-L80", ""),
        ("R0101", "净利润", "MONEY", "SUM", 101, "L33", ""),
    ]:
        mapping.extend(_row(code, label, unit, agg, rnum, monthly, annual))
    return {"reports": {REPORT: {"mapping": mapping}}}


def _values():
    """Consistent baseline: room 100/mo, f&b 50/mo, cost 30/mo; sellable 1000, sold 700."""
    return {
        "R0041": {p: 100 for p in MONTHS} | {"YEAR": 1200},
        "R0049": {p: 50 for p in MONTHS} | {"YEAR": 600},
        "R0080": {p: 30 for p in MONTHS} | {"YEAR": 360},
        "R0032": {p: 150 for p in MONTHS} | {"YEAR": 1800},
        "R0033": {p: 120 for p in MONTHS} | {"YEAR": 1440},
        "R0101": {p: 120 for p in MONTHS} | {"YEAR": 1440},
        "R0024": {p: 1000 for p in MONTHS} | {"YEAR": 12000},
        "R0028": {p: 700 for p in MONTHS} | {"YEAR": 8400},
        "R0029": {p: 0.7 for p in MONTHS} | {"YEAR": 0.7},
    }


class ParseFormulaTests(unittest.TestCase):
    def test_arithmetic(self):
        self.assertIsNotNone(parse_formula("L41+L49"))
        self.assertIsNotNone(parse_formula("L41*L49"))
        self.assertIsNotNone(parse_formula("L41/L49"))

    def test_range_and_funcs(self):
        self.assertIsNotNone(parse_formula("SUM(L45:L48)"))
        self.assertIsNotNone(parse_formula("ROUND(IF(L28=0,0,L41/L28),2)"))
        self.assertIsNotNone(parse_formula("IFERROR(L41/L28,0)"))

    def test_cross_sheet_and_plusplus(self):
        self.assertIsNotNone(parse_formula("'A房务部'!K22 + L74"))
        self.assertIsNotNone(parse_formula("L41++L49"))


class BuildReportGraphTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.n2c = build_report_graph(_manifest(), REPORT)

    def test_classification(self):
        self.assertEqual(self.rows["R0041"]["kind"], "leaf")
        self.assertEqual(self.rows["R0106"]["kind"], "leaf")  # cross-sheet only
        self.assertEqual(self.rows["R0032"]["kind"], "derived")
        self.assertEqual(self.rows["R0029"]["kind"], "derived")

    def test_deps(self):
        self.assertEqual(self.rows["R0032"]["deps"], ["R0041", "R0049"])
        self.assertEqual(self.rows["R0033"]["deps"], ["R0032", "R0080"])
        self.assertEqual(set(self.rows["R0029"]["deps"]), {"R0024", "R0028"})

    def test_row_map(self):
        self.assertEqual(self.n2c[41], "R0041")
        self.assertEqual(self.n2c[32], "R0032")


class RecomputeTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.n2c = build_report_graph(_manifest(), REPORT)
        self.values = _values()

    def test_month_edit_flows_1_to_1(self):
        result = recompute(self.rows, self.n2c, self.values, {"R0041": {"01": 200}})
        self.assertEqual(result["R0032"]["01"], 250)
        self.assertEqual(result["R0033"]["01"], 220)
        self.assertEqual(result["R0101"]["01"], 220)
        self.assertEqual(result["R0032"]["YEAR"], 1900)
        self.assertEqual(result["R0101"]["YEAR"], 1540)

    def test_year_edit_scales_months(self):
        result = recompute(self.rows, self.n2c, self.values, {"R0041": {"YEAR": 2400}})
        self.assertEqual(result["R0041"]["01"], 200)
        self.assertEqual(result["R0032"]["YEAR"], 3000)
        self.assertEqual(result["R0101"]["YEAR"], 2640)

    def test_ratio_derived(self):
        result = recompute(self.rows, self.n2c, self.values, {})
        self.assertAlmostEqual(result["R0029"]["01"], 0.7)
        self.assertAlmostEqual(result["R0029"]["YEAR"], 0.7)

    def test_ratio_zero_guard(self):
        result = recompute(self.rows, self.n2c, self.values, {"R0024": {"01": 0}})
        self.assertEqual(result["R0029"]["01"], 0)

    def test_unchanged_rows_stay_at_baseline(self):
        result = recompute(self.rows, self.n2c, self.values, {})
        for rc in ("R0032", "R0033", "R0101"):
            self.assertEqual(result[rc]["YEAR"], self.values[rc]["YEAR"])


class FullAdjustmentFlowTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(code="P001", name="项目一")
        self.cycle = BudgetCycle.objects.create(name="2026", budget_year=2026, status=BudgetCycle.Status.OPEN)
        self.template = TemplateVersion.objects.create(
            version="V1", budget_year=2026, file_path="", manifest_path="artifacts/template_manifest.json",
            formula_manifest_hash="0" * 64, rule_version="rules-v1", is_active=True,
        )
        self.upload = UploadVersion.objects.create(
            project=self.project, cycle=self.cycle, template=self.template,
            status=UploadVersion.Status.APPROVED, original_path="o.xlsx", sha256="a" * 64,
        )
        ProjectCycle.objects.create(project=self.project, cycle=self.cycle, current_upload=self.upload, is_open=True)
        for month in MONTHS:
            NormalizedValue.objects.create(
                upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0041", row_label="客房收入",
                period=month, unit="MONEY", value_int=100, source_sheet="s", source_cell="c",
            )
        NormalizedValue.objects.create(
            upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0041", row_label="客房收入",
            period="YEAR", unit="MONEY", value_int=1200, source_sheet="s", source_cell="c",
        )

    def test_create_and_issue_full_adjustment(self):
        batch = create_full_adjustment(self.cycle, self.project, "PL_TOTAL_WINE", {"R0041": {"YEAR": 2400}}, "调整客房收入")
        self.assertEqual(batch.driver_label, "全表调整")
        self.assertEqual(batch.cascade["kind"], "full")
        self.assertEqual(batch.lines.count(), 1)
        line = batch.lines.first()
        self.assertEqual(line.row_code, "R0041")
        self.assertEqual(line.period, "YEAR")
        self.assertEqual(line.baseline_cents, 1200)
        self.assertEqual(line.target_cents, 2400)
        self.assertEqual(line.allocated_delta_cents, 1200)
        self.assertEqual(batch.delta_cents, 1200)

        row41 = next(r for r in batch.cascade["rows"] if r[0] == "R0041")
        self.assertEqual(row41[4], 2400)

        issue_adjustment(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.status, AdjustmentBatch.Status.ISSUED)

    def test_reupload_confirms_line(self):
        batch = create_full_adjustment(self.cycle, self.project, "PL_TOTAL_WINE", {"R0041": {"YEAR": 2400}}, "调整")
        issue_adjustment(batch)
        line = batch.lines.first()
        new_upload = UploadVersion.objects.create(
            project=self.project, cycle=self.cycle, template=self.template,
            status=UploadVersion.Status.APPROVED, original_path="o2.xlsx", sha256="b" * 64,
        )
        NormalizedValue.objects.create(
            upload=new_upload, report_code="PL_TOTAL_WINE", row_code="R0041", row_label="客房收入",
            period="YEAR", unit="MONEY", value_int=2400, source_sheet="s", source_cell="c",
        )
        update_adjustment_lines_for_upload(new_upload)
        line.refresh_from_db()
        self.assertEqual(line.status, AdjustmentLine.Status.CONFIRMED)

    def test_reject_derived_edit(self):
        with self.assertRaises(ValueError):
            create_full_adjustment(self.cycle, self.project, "PL_TOTAL_WINE", {"R0032": {"YEAR": 1}}, "非法")


class FullAdjustmentViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("admin", password="x", role="ADMIN", is_staff=True)
        self.project = Project.objects.create(code="P001", name="项目一")
        self.puser = User.objects.create_user("p001", password="x", role="PROJECT", project=self.project)
        BudgetCycle.objects.create(name="2026", budget_year=2026, status=BudgetCycle.Status.OPEN)

    def test_management_adjustments_200(self):
        client = Client()
        client.force_login(self.admin)
        response = client.get("/management/adjustments/", HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)

    def test_project_adjustments_200(self):
        client = Client()
        client.force_login(self.puser)
        response = client.get("/project/adjustments/", HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
