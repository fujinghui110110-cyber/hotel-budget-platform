from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from budgeting.planning_views import _comparison
from budgeting.services.metrics import rolling_years


class PlanningComparisonTests(SimpleTestCase):
    def comparison(self, code, current, previous):
        with patch("budgeting.planning_views.aggregate_metric", side_effect=[current, previous]):
            return _comparison(SimpleNamespace(budget_year=2028), code, "PL_TOTAL_WINE")

    def test_different_project_coverage_cannot_be_compared(self):
        result = self.comparison("revenue_total", {"value": 200, "project_ids": [1, 2]},
                                 {"value": 100, "project_ids": [1]})
        self.assertEqual(result, ("—", "对照项目不一致"))

    def test_missing_comparison_is_not_zero(self):
        result = self.comparison("revenue_total", {"value": 200, "project_ids": [1]},
                                 {"value": None, "project_ids": []})
        self.assertEqual(result, ("—", "缺少对照数据"))

    def test_occupancy_change_uses_percentage_points(self):
        result = self.comparison("occ", {"value": 7550, "project_ids": [1]},
                                 {"value": 7000, "project_ids": [1]})
        self.assertEqual(result, ("+5.50 个百分点", "—"))

    def test_zero_base_has_no_infinite_growth(self):
        result = self.comparison("revenue_total", {"value": 1000000, "project_ids": [1]},
                                 {"value": 0, "project_ids": [1]})
        self.assertEqual(result[1], "基期为零")

    def test_rolling_years_follow_selected_budget(self):
        for year in (2027, 2028, 2032):
            result = rolling_years(year)
            self.assertEqual(result,
                             [(year - 3, "ACTUAL"), (year - 2, "ACTUAL"),
                              (year - 1, "FORECAST"), (year, "BUDGET")])
