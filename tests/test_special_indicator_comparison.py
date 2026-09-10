from django.test import TestCase

from budgeting.models import (BudgetCycle, IndicatorProject, NormalizedValue, Project,
                              SpecialIndicatorBatch, SpecialIndicatorValue, TemplateVersion, UploadVersion)
from budgeting.services.special_indicator_comparison import comparison_data


class SpecialIndicatorComparisonTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(code='SPECIALQA', name='专项测试酒店')
        self.indicator_project = IndicatorProject.objects.create(name=self.project.name, project=self.project)
        template = TemplateVersion.objects.create(version='SPECIALQA', budget_year=2027)
        self.cycle = BudgetCycle.objects.create(name='专项测试', budget_year=2027, template=template)
        self.upload = UploadVersion.objects.create(project=self.project, cycle=self.cycle,
                                                  template=template, status='SUBMITTED')

    def value(self, period, amount, label='餐饮收入', row_code='S0024'):
        return NormalizedValue.objects.create(upload=self.upload, report_code='B3宴会厅',
                                              row_code=row_code, row_label=label, period=period,
                                              unit='MONEY', value_int=amount)

    def banquet(self):
        rows = comparison_data(year=2027, data_type='BUDGET', cycle=self.cycle)['rows']
        return next(row for row in rows if row['indicator'] == 'BANQUET')

    def test_budget_source_retains_zero_missing_months_and_annual(self):
        self.value('01', 0)
        self.value('02', 12345)
        self.value('YEAR', 12345)
        row = self.banquet()
        self.assertEqual(row['values'][:3], [0, 123.45, None])
        self.assertEqual(row['total'], 123.45)
        self.assertFalse(row['complete'])
        self.assertTrue(row['annual_complete'])
        self.assertEqual(row['source'], 'budget_workbook')
        self.assertIn('餐饮收入', row['source_message'])

    def test_same_named_expense_and_ambiguous_revenue_are_not_used(self):
        self.value('YEAR', 1200, label='餐饮成本')
        self.assertIsNone(self.banquet()['total'])
        self.value('YEAR', 1200, row_code='DUP1')
        self.value('YEAR', 1300, row_code='DUP2')
        row = self.banquet()
        self.assertIsNone(row['total'])
        self.assertIn('多个同名', row['source_message'])

    def test_wrong_period_and_failed_newer_upload_do_not_replace_source(self):
        self.value('A2025', 9900)
        self.assertIsNone(self.banquet()['total'])
        self.value('YEAR', 15000)
        UploadVersion.objects.create(project=self.project, cycle=self.cycle,
                                     template=self.cycle.template, status='REJECTED')
        self.assertEqual(self.banquet()['total'], 150)
        self.assertEqual(comparison_data(year=2027, data_type='BUDGET', cycle=self.cycle,
                                         project_ids=[])['rows'], [])

    def test_empty_supplement_does_not_hide_budget_but_explicit_zero_overrides(self):
        self.value('YEAR', 15000)
        batch = SpecialIndicatorBatch.objects.create(year=2027, data_type='BUDGET', cycle=self.cycle, unit='YUAN')
        item = SpecialIndicatorValue.objects.create(batch=batch, project=self.indicator_project,
                                                    indicator='BANQUET', month=1, value=None)
        self.assertEqual(self.banquet()['total'], 150)
        item.value = 0
        item.save()
        row = self.banquet()
        self.assertEqual(row['values'][0], 0)
        self.assertIsNone(row['total'])
        self.assertEqual(row['source'], 'special_indicator_template')
