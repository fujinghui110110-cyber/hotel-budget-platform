import io

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from budgeting.management.commands.seed_planning_demo import month_values
from budgeting.models import NormalizedValue, Project


@override_settings(DEBUG=True)
class SeedPlanningDemoTests(TestCase):
    def test_seed_adds_four_year_supplementary_metrics_without_double_counting_income(self):
        call_command("seed_planning_demo", year=2027, stdout=io.StringIO())

        self.assertEqual(Project.objects.count(), 3)
        upload = Project.objects.get(code="DEMO01").uploadversion_set.get()
        expected_codes = {"R9001", "R9002", "R9003", "R9004", "R9005"}
        for row_code in expected_codes:
            values = NormalizedValue.objects.filter(
                upload=upload,
                report_code="PL_TOTAL_WINE",
                row_code=row_code,
            )
            self.assertEqual(values.count(), 52)
            self.assertEqual(values.filter(month__isnull=False).count(), 48)
            self.assertEqual(values.filter(month__isnull=True).count(), 4)
            self.assertEqual(list(values.values_list("source_cell", flat=True).distinct()), ["DEMO"])
            self.assertEqual(
                list(values.values_list("source_sheet", flat=True).distinct()),
                ["合成演示数据（无真实项目来源）"],
            )

        total = NormalizedValue.objects.get(
            upload=upload,
            report_code="PL_TOTAL_WINE",
            row_code="R0032",
            period="01",
        )
        self.assertEqual(total.value_int, month_values("PL_TOTAL_WINE", 2027, 1, 0)[32])
        income_parts = NormalizedValue.objects.filter(
            upload=upload,
            report_code="PL_TOTAL_WINE",
            row_code__in=("R9001", "R9002", "R9003", "R9005"),
            period="01",
        )
        self.assertNotEqual(total.value_int, sum(value.value_int for value in income_parts))

    def test_seed_refuses_nonempty_database(self):
        Project.objects.create(code="EXISTING", name="已有项目")

        with self.assertRaises(CommandError):
            call_command("seed_planning_demo", year=2027, stdout=io.StringIO())

        self.assertEqual(Project.objects.count(), 1)
