from django.test import TestCase
from unittest.mock import patch

from budgeting.excel.business_checks import validate_management_values
from budgeting.models import BudgetCycle, NormalizedValue, Project, UploadVersion, ValidationRun


class BusinessChecksTests(TestCase):
    def test_variance_is_disabled_by_default_and_is_only_p1_when_enabled(self):
        from budgeting.excel.business_checks import validate_variances
        value = self.value(41, 250)
        value.data_year, value.data_kind, value.month = 2027, 'BUDGET', 1
        value.save()
        self.upload.cycle.budget_year = 2027
        NormalizedValue.objects.create(upload=self.upload, report_code='PL_TOTAL_WINE', row_code='R0041', period='F2026M01', unit='MONEY', value_int=100, data_year=2026, data_kind='FORECAST', month=1)
        manifest = {'reports': {'PL_TOTAL_WINE': {'mapping': [{'row_code': 'R0041', 'unit': 'MONEY', 'aggregation': 'SUM'}]}}}
        with patch('budgeting.excel.business_checks._load_manifest', return_value=manifest):
            validate_variances(self.upload, self.run)
            self.assertFalse(self.run.issues.exists())
            self.upload.cycle.p1_threshold_cents = 100
            validate_variances(self.upload, self.run)
        self.assertEqual(self.run.issues.get().severity, 'P1')

    def setUp(self):
        project = Project.objects.create(code='CHECK', name='校验项目')
        cycle = BudgetCycle.objects.create(name='2027预算', budget_year=2027)
        self.upload = UploadVersion.objects.create(project=project, cycle=cycle, original_path='test.xlsx', sha256='0' * 64)
        self.run = ValidationRun.objects.create(upload=self.upload, rule_version='R2')

    def value(self, row, amount, report='PL_TOTAL_WINE', unit='MONEY'):
        return NormalizedValue.objects.create(upload=self.upload, report_code=report, row_code=f'R{row:04d}', period='01', unit=unit, value_int=amount, source_sheet=report, source_cell=f'L{row}')

    def test_profit_one_cent_blocks(self):
        for row, amount in [(41, 10000), (42, 2000), (43, 500), (44, 7501)]:
            self.value(row, amount)
        validate_management_values(self.upload, self.run)
        self.assertEqual(self.run.issues.get(code='PROFIT_RECONCILIATION').expected_value, '7500')

    def test_npi_reverse_distinct_from_operating_profit(self):
        for row, amount in [(101, 10000), (100, 2500), (96, 300), (93, 200), (105, 13000), (90, 18000)]:
            self.value(row, amount)
        validate_management_values(self.upload, self.run)
        self.assertFalse(self.run.issues.exists())
        NormalizedValue.objects.filter(upload=self.upload, row_code='R0105').update(value_int=13001)
        validate_management_values(self.upload, self.run)
        self.assertTrue(self.run.issues.filter(code='NPI_REVERSE').exists())

    def test_split_npi_reverse(self):
        for row, amount in [(127, 100), (126, 25), (120, 30), (117, 20), (123, 10), (129, 165)]:
            self.value(row, amount, report='PL_ZZ_WINE')
        validate_management_values(self.upload, self.run)
        self.assertFalse(self.run.issues.exists())

    def test_price_half_up_and_room_capacity(self):
        self.value(41, 101)
        self.value(30, 51)
        self.value(28, 2, unit='COUNT')
        self.value(24, 1, unit='COUNT')
        validate_management_values(self.upload, self.run)
        self.assertTrue(self.run.issues.filter(code='ROOM_NIGHTS_CAPACITY').exists())
        self.assertFalse(self.run.issues.filter(code='ROOM_PRICE_RECONCILIATION').exists())
