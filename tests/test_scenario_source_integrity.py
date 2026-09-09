from copy import deepcopy

from django.test import SimpleTestCase, TestCase

from budgeting.models import BudgetCycle
from budgeting.services.scenarios import ScenarioError, _validate_source, calculate_scenario, clone_scenario, MONTHS
from tests.test_scenarios_v2 import _full_report_baseline, ScenarioFixtureMixin


class ScenarioSourceIntegrityTests(SimpleTestCase):
    def test_missing_month_and_one_cent_profit_difference_are_rejected(self):
        report = "PL_TOTAL_WINE"
        tables = {report: _full_report_baseline(report)}
        _validate_source(tables)
        incomplete = deepcopy(tables)
        del incomplete[report]["R0041"]["before"]["01"]
        with self.assertRaisesMessage(ScenarioError, "不能由年度数均摊"):
            _validate_source(incomplete)
        inconsistent = deepcopy(tables)
        inconsistent[report]["R0044"]["before"]["01"]["value_int"] += 1
        with self.assertRaisesMessage(ScenarioError, "基线勾稽不一致"):
            _validate_source(inconsistent)


class ScenarioFailurePersistenceTests(ScenarioFixtureMixin, TestCase):
    def test_failed_recalculation_clears_old_ready_results(self):
        scenario = clone_scenario(project=self.project, cycle=self.cycle, name="失败持久化", created_by=self.admin)
        scenario.status, scenario.results = "READY", {"old": True}
        scenario.save()
        with self.assertRaises(ScenarioError):
            calculate_scenario(scenario, inputs={"driver": "OCC", "monthly": {m: "150" for m in MONTHS}}, actor=self.admin)
        scenario.refresh_from_db()
        self.assertEqual(scenario.status, "FAILED")
        self.assertEqual(scenario.results, {})
        self.assertTrue(scenario.error)

    def test_frozen_cycle_rejection_does_not_mutate_scenario(self):
        scenario = clone_scenario(project=self.project, cycle=self.cycle, name="冻结只读", created_by=self.admin)
        scenario.status, scenario.results = "READY", {"approved_context": True}
        scenario.save()
        self.cycle.status = BudgetCycle.Status.FROZEN
        self.cycle.save()
        with self.assertRaisesMessage(ScenarioError, "冻结"):
            calculate_scenario(scenario, actor=self.admin)
        scenario.refresh_from_db()
        self.assertEqual(scenario.status, "READY")
        self.assertEqual(scenario.results, {"approved_context": True})
