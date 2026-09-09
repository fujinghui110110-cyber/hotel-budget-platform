from django.test import TestCase

from budgeting.excel.money import largest_remainder
from budgeting.models import BudgetCycle, Project, ProjectCycle
from budgeting.services import freeze_cycle


class WorkflowTests(TestCase):
    def test_freeze_requires_current_versions(self):
        Project.objects.create(code="P001", name="XX项目")
        cycle = BudgetCycle.objects.create(name="2026预算", budget_year=2026, revision_no=1, status=BudgetCycle.OPEN)
        with self.assertRaisesRegex(ValueError, "无正式版本"):
            freeze_cycle(cycle, None)

    def test_project_cycle_unique(self):
        project = Project.objects.create(code="P001", name="XX项目")
        cycle = BudgetCycle.objects.create(name="2026预算", budget_year=2026, revision_no=1)
        ProjectCycle.objects.create(project=project, cycle=cycle)
        with self.assertRaises(Exception):
            ProjectCycle.objects.create(project=project, cycle=cycle)

    def test_equal_zero_weight_adjustment_allocation(self):
        self.assertEqual(largest_remainder(10000, {"B": 0, "A": 0, "C": 0}), {"A": 3334, "B": 3333, "C": 3333})
