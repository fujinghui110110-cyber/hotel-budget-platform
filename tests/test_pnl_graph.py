import unittest
from decimal import Decimal
from unittest.mock import patch

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
    _eval,
    annual_formula_overrides_rollup,
    build_external_values,
    build_report_graph,
    load_external_values,
    parse_formula,
    recompute,
    required_external_refs,
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


def _manifest(report_code=REPORT, include_external=True):
    mapping = []
    rows = [
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
    ]
    if include_external:
        rows.append(("R0034", "外部调整后利润", "MONEY", "SUM", 34, "L33+'外部表'!K22", ""))
    for code, label, unit, agg, rnum, monthly, annual in rows:
        mapping.extend(_row(code, label, unit, agg, rnum, monthly, annual))
    return {"reports": {report_code: {"mapping": mapping}}}


def _values():
    """Consistent baseline: room 100/mo, f&b 50/mo, cost 30/mo; sellable 1000, sold 700."""
    return {
        "R0041": {p: 100 for p in MONTHS} | {"YEAR": 1200},
        "R0049": {p: 50 for p in MONTHS} | {"YEAR": 600},
        "R0080": {p: 30 for p in MONTHS} | {"YEAR": 360},
        "R0032": {p: 150 for p in MONTHS} | {"YEAR": 1800},
        "R0033": {p: 120 for p in MONTHS} | {"YEAR": 1440},
        "R0034": {p: 125 for p in MONTHS} | {"YEAR": 1500},
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
        result = recompute(self.rows, self.n2c, self.values, {"R0041": {"01": 200}}, external_values={"'外部表'!K22": 5})
        self.assertEqual(result["R0032"]["01"], 250)
        self.assertEqual(result["R0033"]["01"], 220)
        self.assertEqual(result["R0101"]["01"], 220)
        self.assertEqual(result["R0032"]["YEAR"], 1900)
        self.assertEqual(result["R0101"]["YEAR"], 1540)

    def test_year_edit_requires_explicit_allocation(self):
        with self.assertRaisesRegex(ValueError, "年度目标不会自动摊月"):
            recompute(self.rows, self.n2c, self.values, {"R0041": {"YEAR": 2400}})

    def test_explicit_year_allocation_preserves_total(self):
        result = recompute(
            self.rows,
            self.n2c,
            self.values,
            {"R0041": {"YEAR": 2400}},
            external_values={"'外部表'!K22": 5},
            allow_year_allocation=True,
        )
        self.assertEqual(result["R0041"]["01"], 200)
        self.assertEqual(result["R0032"]["YEAR"], 3000)
        self.assertEqual(result["R0101"]["YEAR"], 2640)
        self.assertEqual(sum(result["R0041"][p] for p in MONTHS), 2400)

    def test_ratio_derived(self):
        result = recompute(self.rows, self.n2c, self.values, {}, external_values={"'外部表'!K22": 5})
        self.assertEqual(result["R0029"]["01"], Decimal("0.7"))
        self.assertEqual(result["R0029"]["YEAR"], Decimal("0.7"))

    def test_ratio_zero_guard(self):
        result = recompute(self.rows, self.n2c, self.values, {"R0024": {"01": 0}}, external_values={"'外部表'!K22": 5})
        self.assertEqual(result["R0029"]["01"], 0)

    def test_unchanged_rows_stay_at_baseline(self):
        result = recompute(self.rows, self.n2c, self.values, {}, external_values={"'外部表'!K22": 5})
        for rc in ("R0032", "R0033", "R0034", "R0101"):
            self.assertEqual(result[rc]["YEAR"], self.values[rc]["YEAR"])

    def test_external_reference_requires_verified_constant(self):
        with self.assertRaisesRegex(ValueError, "未验证外部引用"):
            recompute(self.rows, self.n2c, self.values, {"R0041": {"01": 200}})

    def test_baseline_mismatch_raises_instead_of_residual(self):
        values = {rc: dict(per) for rc, per in self.values.items()}
        values["R0033"]["01"] = 121
        with self.assertRaisesRegex(ValueError, "公式勾稽不平"):
            recompute(self.rows, self.n2c, values, {}, external_values={"'外部表'!K22": 5})

    def test_unaffected_external_formula_does_not_block_unrelated_edit(self):
        manifest = _manifest(include_external=False)
        manifest["reports"][REPORT]["mapping"].extend(
            _row("R0074", "广告费", "MONEY", "SUM", 74, "", "")
            + _row("R0075", "市场销售（管理）", "MONEY", "SUM", 75, "E3市场销售!K29-L74++('明细表'!L75)", "")
        )
        rows, n2c = build_report_graph(manifest, REPORT)
        values = _values()
        values["R0074"] = {p: 10 for p in MONTHS} | {"YEAR": 120}
        values["R0075"] = {p: 90 for p in MONTHS} | {"YEAR": 1080}
        result = recompute(rows, n2c, values, {"R0041": {"01": 200}})
        self.assertEqual(result["R0075"]["01"], 90)
        self.assertEqual(result["R0075"]["YEAR"], 1080)

    def test_affected_external_formula_still_requires_verified_source(self):
        manifest = _manifest(include_external=False)
        manifest["reports"][REPORT]["mapping"].extend(
            _row("R0074", "广告费", "MONEY", "SUM", 74, "", "")
            + _row("R0075", "市场销售（管理）", "MONEY", "SUM", 75, "E3市场销售!K29-L74++('明细表'!L75)", "")
        )
        rows, n2c = build_report_graph(manifest, REPORT)
        values = _values()
        values["R0074"] = {p: 10 for p in MONTHS} | {"YEAR": 120}
        values["R0075"] = {p: 90 for p in MONTHS} | {"YEAR": 1080}
        with self.assertRaisesRegex(ValueError, "未验证外部引用"):
            recompute(rows, n2c, values, {"R0074": {"01": 20}})

    def test_external_values_adapter_scopes_to_affected_formulas(self):
        manifest = _manifest(include_external=False)
        manifest["reports"][REPORT]["mapping"].extend(
            _row("R0074", "广告费", "MONEY", "SUM", 74, "", "")
            + _row("R0075", "市场销售（管理）", "MONEY", "SUM", 75, "E3市场销售!K29-L74++('明细表'!L75)", "")
        )
        rows, _n2c = build_report_graph(manifest, REPORT)

        self.assertEqual(required_external_refs(rows, {"R0041": {"01": 200}}), ())
        self.assertEqual(
            required_external_refs(rows, {"R0074": {"01": 20}}),
            ("E3市场销售!K29", "'明细表'!L75"),
        )
        self.assertEqual(build_external_values(rows, {"R0041": {"01": 200}}), {})
        with self.assertRaisesRegex(ValueError, "E3市场销售!K29"):
            build_external_values(rows, {"R0074": {"01": 20}}, {"'明细表'!L75": 100})
        self.assertEqual(
            build_external_values(
                rows,
                {"R0074": {"01": 20}},
                {"E3市场销售!K29": 130, "'明细表'!L75": 100},
            ),
            {"E3市场销售!K29": 130, "'明细表'!L75": 100},
        )

    def test_count_function_supports_blank_guard_formulas(self):
        ast = parse_formula("IF(COUNT(L23:W23)=0,0,ROUND(AVERAGE(L23:W23),0))")
        result = _eval(
            ast,
            lambda _col, _row: Decimal(0),
            lambda *_args: [Decimal("10"), Decimal("20"), Decimal("30")],
        )
        self.assertEqual(result, Decimal("20"))

    def test_non_simple_annual_formula_overrides_monthly_sum(self):
        def zz_row(code, label, unit, agg, rnum, monthly, annual):
            return [
                {
                    "row_code": code,
                    "row_label": label,
                    "unit": unit,
                    "aggregation": agg,
                    "formula": monthly,
                    "source": {"cell": f"F{rnum}"},
                },
                {
                    "row_code": code,
                    "row_label": label,
                    "unit": unit,
                    "aggregation": agg,
                    "formula": annual,
                    "source": {"cell": f"S{rnum}"},
                },
            ]

        manifest = {"reports": {"PL_ZZ_TEST": {"mapping": []}}}
        for row_num in [113, 115, 116, 117, 118, 119, 120, 121, 122, 123]:
            manifest["reports"]["PL_ZZ_TEST"]["mapping"].extend(
                zz_row(f"R{row_num:04d}", f"R{row_num:04d}", "MONEY", "SUM", row_num, "", "")
            )
        manifest["reports"]["PL_ZZ_TEST"]["mapping"].extend(
            zz_row(
                "R0124",
                "年度桥接行",
                "MONEY",
                "SUM",
                124,
                "F113",
                "ROUND(S113+S121-SUM(S115:S120,S122)+S123,2)",
            )
        )
        rows, n2c = build_report_graph(manifest, "PL_ZZ_TEST")
        values = {
            f"R{row:04d}": {month: Decimal(1) for month in MONTHS} | {"YEAR": Decimal(row)}
            for row in [113, 115, 116, 117, 118, 119, 120, 121, 122, 123]
        }
        values["R0124"] = {month: Decimal(0) for month in MONTHS} | {"YEAR": Decimal(0)}

        result = recompute(rows, n2c, values, {"R0113": {"01": Decimal(200)}})

        expected = result["R0113"]["YEAR"] + Decimal(121) - sum(Decimal(row) for row in [115, 116, 117, 118, 119, 120, 122]) + Decimal(123)
        self.assertEqual(result["R0124"]["YEAR"], expected)
        self.assertTrue(annual_formula_overrides_rollup(rows["R0124"]))

    def test_simple_rounded_month_sum_keeps_rollup_path(self):
        manifest = {"reports": {"PL_ZZ_TEST": {"mapping": []}}}
        manifest["reports"]["PL_ZZ_TEST"]["mapping"].extend([
            {
                "row_code": "R0105",
                "row_label": "奖励管理费",
                "unit": "MONEY",
                "aggregation": "SUM",
                "formula": "F103*0.04",
                "source": {"cell": "F105"},
            },
            {
                "row_code": "R0105",
                "row_label": "奖励管理费",
                "unit": "MONEY",
                "aggregation": "SUM",
                "formula": "ROUND(SUM(ROUND(F105,2),ROUND(G105,2),ROUND(H105,2),ROUND(I105,2),ROUND(J105,2),ROUND(K105,2),ROUND(L105,2),ROUND(M105,2),ROUND(N105,2),ROUND(O105,2),ROUND(P105,2),ROUND(Q105,2)),2)",
                "source": {"cell": "S105"},
            },
        ])
        rows, n2c = build_report_graph(manifest, "PL_ZZ_TEST")
        values = {"R0105": {month: Decimal(10) for month in MONTHS} | {"YEAR": Decimal(120)}}

        result = recompute(rows, n2c, values, {"R0105": {"01": Decimal(20)}})

        self.assertFalse(annual_formula_overrides_rollup(rows["R0105"]))
        self.assertEqual(result["R0105"]["YEAR"], Decimal(130))


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
        with patch("budgeting.services.workflow.load_manifest", return_value=_manifest("PL_TOTAL_WINE", include_external=False)):
            batch = create_full_adjustment(self.cycle, self.project, "PL_TOTAL_WINE", {"R0041": {"01": 200}}, "调整客房收入")

        self.assertEqual(batch.driver_label, "全表调整")
        self.assertEqual(batch.cascade["kind"], "full")
        self.assertEqual(batch.lines.count(), 1)
        line = batch.lines.first()
        self.assertEqual(line.row_code, "R0041")
        self.assertEqual(line.period, "01")
        self.assertEqual(line.baseline_cents, 100)
        self.assertEqual(line.target_cents, 200)
        self.assertEqual(line.allocated_delta_cents, 100)
        self.assertEqual(batch.delta_cents, 100)

        row41 = next(r for r in batch.cascade["rows"] if r[0] == "R0041")
        self.assertEqual(row41[4], 1300)

        issue_adjustment(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.status, AdjustmentBatch.Status.ISSUED)

    def test_reupload_confirms_line(self):
        with patch("budgeting.services.workflow.load_manifest", return_value=_manifest("PL_TOTAL_WINE", include_external=False)):
            batch = create_full_adjustment(self.cycle, self.project, "PL_TOTAL_WINE", {"R0041": {"01": 200}}, "调整")
        issue_adjustment(batch)
        line = batch.lines.first()
        new_upload = UploadVersion.objects.create(
            project=self.project, cycle=self.cycle, template=self.template,
            status=UploadVersion.Status.APPROVED, original_path="o2.xlsx", sha256="b" * 64,
        )
        NormalizedValue.objects.create(
            upload=new_upload, report_code="PL_TOTAL_WINE", row_code="R0041", row_label="客房收入",
            period="01", unit="MONEY", value_int=200, source_sheet="s", source_cell="c",
        )
        update_adjustment_lines_for_upload(new_upload)
        line.refresh_from_db()
        self.assertEqual(line.status, AdjustmentLine.Status.CONFIRMED)

    def test_reject_derived_edit(self):
        with patch("budgeting.services.workflow.load_manifest", return_value=_manifest("PL_TOTAL_WINE", include_external=False)):
            with self.assertRaisesRegex(ValueError, "联动计算项"):
                create_full_adjustment(self.cycle, self.project, "PL_TOTAL_WINE", {"R0032": {"YEAR": 1}}, "非法")

    def test_load_external_values_uses_normalized_source_cells(self):
        NormalizedValue.objects.create(
            upload=self.upload,
            report_code="PL_TOTAL_WINE",
            row_code="R0999",
            row_label="外部缓存",
            period="01",
            unit="MONEY",
            value_int=12345,
            source_sheet="外部表",
            source_cell="K22",
        )

        self.assertEqual(load_external_values(self.upload, ("'外部表'!K22",)), {"'外部表'!K22": 12345})
        with self.assertRaisesRegex(ValueError, "UNVERIFIED"):
            load_external_values(self.upload, ("'外部表'!K23",))



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
