from copy import deepcopy

from django.test import SimpleTestCase, TestCase

from budgeting.excel.business_checks import TOTAL_RULES
from budgeting.models import NormalizedValue
from budgeting.services.scenarios import MONTHS, ScenarioError, calculate_fixed_cost_scenario
from tests.test_scenarios_v2 import REPORT_ROWS, ScenarioFixtureMixin, _full_report_baseline
from budgeting.services.scenarios import calculate_scenario, clone_scenario, issue_scenario


class ScenarioCostModeTests(SimpleTestCase):
    def setUp(self):
        self.baseline = {"PL_TOTAL_WINE": _full_report_baseline("PL_TOTAL_WINE")}

    def test_proportional_costs_follow_budget_room_revenue_and_registered_profit_rules(self):
        original = deepcopy(self.baseline)
        result = calculate_fixed_cost_scenario(
            self.baseline,
            {"driver": "ROOM_REV", "cost_mode": "proportional", "monthly": {month: 80_000 for month in MONTHS}},
            budget_year=2026,
        )
        report = result["reports"]["PL_TOTAL_WINE"]
        rows = {row["row_code"]: row for row in report["rows"]}

        self.assertFalse(result["costs_fixed"])
        self.assertEqual(result["cost_mode"], "proportional")
        self.assertEqual(result["inputs"]["cost_mode"], "proportional")
        self.assertEqual(result["assumptions"]["profit_linkage"], "registered_rules")
        self.assertEqual(result["assumptions"]["cost_mode"], "proportional")
        self.assertEqual(result["cost_linkage"]["reports"]["PL_TOTAL_WINE"]["cost_rows"], ["R0042", "R0043"])
        self.assertEqual(rows["R0042"]["after"]["01"]["value_int"], 1_143)
        self.assertEqual(rows["R0043"]["after"]["01"]["value_int"], 1_143)
        self.assertEqual(rows["R0042"]["after"]["YEAR"]["value_int"], 1_143 * 12)
        self.assertEqual(rows["R0043"]["after"]["YEAR"]["value_int"], 1_143 * 12)

        for target, dependencies in TOTAL_RULES.items():
            target_row = rows[f"R{target:04d}"]
            expected = sum(rows[f"R{row:04d}"]["after"]["01"]["value_int"] * sign for row, sign in dependencies)
            self.assertEqual(target_row["after"]["01"]["value_int"], expected, f"R{target:04d} profit rule")
        self.assertEqual(rows["R0033"]["after"]["01"]["value_int"], rows["R0080"]["after"]["01"]["value_int"])
        self.assertNotEqual(rows["R0105"]["after"]["01"]["value_int"], rows["R0105"]["before"]["01"]["value_int"])
        self.assertEqual(self.baseline, original)

    def test_omitted_cost_mode_keeps_fixed_legacy_semantics(self):
        result = calculate_fixed_cost_scenario(
            self.baseline,
            {"driver": "ROOM_REV", "monthly": {month: 80_000 for month in MONTHS}},
            budget_year=2026,
        )
        self.assertEqual(result["cost_mode"], "fixed")
        self.assertEqual(result["inputs"]["cost_mode"], "fixed")
        self.assertEqual(result["assumptions"]["cost_mode"], "fixed")
        self.assertEqual(result["assumptions"]["profit_linkage"], "room_revenue_delta_1_to_1")

    def test_proportional_mode_rejects_missing_traceable_room_cost_source(self):
        baseline = deepcopy(self.baseline)
        del baseline["PL_TOTAL_WINE"]["R0043"]
        with self.assertRaisesMessage(ScenarioError, "缺少 proportional 客房收入/成本来源行"):
            calculate_fixed_cost_scenario(
                baseline,
                {"driver": "ROOM_REV", "cost_mode": "proportional", "monthly": {month: 80_000 for month in MONTHS}},
                budget_year=2026,
            )

    def test_non_empty_fixed_costs_are_rejected_instead_of_ignored(self):
        with self.assertRaisesMessage(ScenarioError, "fixed_costs 暂不支持直接覆盖"):
            calculate_fixed_cost_scenario(
                self.baseline,
                {"driver": "ROOM_REV", "monthly": {month: 80_000 for month in MONTHS}, "fixed_costs": {"R0042": 123}},
                budget_year=2026,
            )


class ProportionalScenarioIssueTests(ScenarioFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        from budgeting.services.workflow import _project_report

        graph_codes = {}
        for report_code in REPORT_ROWS:
            graph, _numbers, _values, _order, _upload = _project_report(self.project, self.cycle, report_code)
            graph_codes[report_code] = set(graph)
        NormalizedValue.objects.filter(upload=self.upload).delete()
        for report_code in REPORT_ROWS:
            for row_code, row in _full_report_baseline(report_code).items():
                if row_code not in graph_codes[report_code]:
                    continue
                for period in (*MONTHS, "YEAR"):
                    cell = row["before"][period]
                    NormalizedValue.objects.create(
                        upload=self.upload,
                        report_code=report_code,
                        row_code=row_code,
                        row_label=row_code,
                        period=period,
                        unit=cell["unit"],
                        value_int=cell["value_int"],
                        ratio_num=cell.get("ratio_num"),
                        ratio_den=cell.get("ratio_den"),
                        source_sheet=report_code,
                        source_cell="C1",
                    )

    def test_four_report_proportional_scenario_issues_all_cost_and_target_leaves(self):
        scenario = clone_scenario(
            project=self.project,
            cycle=self.cycle,
            name="比例成本四表联动",
            created_by=self.admin,
        )
        calculate_scenario(
            scenario,
            {
                "driver": "ROOM_REV",
                "cost_mode": "proportional",
                "monthly": {month: "80000" for month in MONTHS},
            },
            actor=self.admin,
        )
        scenario.refresh_from_db()
        batch = issue_scenario(scenario, actor=self.admin)
        self.assertEqual(batch.status, "ISSUED")
        self.assertEqual(set(batch.lines.values_list("report_code", flat=True)), set(REPORT_ROWS))
        for report_code in REPORT_ROWS:
            codes = set(batch.lines.filter(report_code=report_code).values_list("row_code", flat=True))
            if report_code.startswith("PL_ZZ"):
                self.assertTrue({"R0021", "R0023", "R0024", "R0025", "R0026", "R0027"}.issubset(codes))
            else:
                self.assertTrue({"R0041", "R0042", "R0043"}.issubset(codes))
            posted_rows = {
                row[0]: row
                for row in batch.cascade["reports"][report_code]["posted"]["rows"]
            }
            for result_row in scenario.results["reports"][report_code]["rows"]:
                if not result_row.get("changed"):
                    continue
                posted = posted_rows[result_row["row_code"]]
                expected = result_row["after"]["YEAR"]
                if expected["unit"] == "RATIO":
                    self.assertAlmostEqual(
                        posted[4],
                        expected["ratio_num"] / expected["ratio_den"],
                        msg=f"{report_code}:{result_row['row_code']}",
                    )
                else:
                    self.assertEqual(posted[4], expected["value_int"], f"{report_code}:{result_row['row_code']}")
        self.assertEqual(
            batch.cascade["report_deltas_cents"],
            {
                report_code: sum(
                    line.allocated_delta_cents
                    for line in batch.lines.filter(report_code=report_code)
                )
                for report_code in REPORT_ROWS
            },
        )
