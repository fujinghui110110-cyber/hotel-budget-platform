import tempfile
from decimal import Decimal
from pathlib import Path

from django.test import TestCase, override_settings
from openpyxl import Workbook
from budgeting.models import (User, Project, IndicatorProject, BudgetCycle, TemplateVersion,
    UploadVersion, NormalizedValue, ManagementMetricBatch, ManagementMetricValue)
from budgeting.services.management_metrics import preview_import, confirm_import, comparison_data


class ManagementMetricsTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.temp.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user = User.objects.create_user(username='manager', role='ADMIN')
        self.project = Project.objects.create(code='MTEST', name='测试酒店')
        self.identity = IndicatorProject.objects.create(name='测试', project=self.project)
        template = TemplateVersion.objects.create(version='METRICSTEST', budget_year=2030)
        self.cycle = BudgetCycle.objects.create(name='2030 R1', budget_year=2030, template=template)
        self.upload = UploadVersion.objects.create(cycle=self.cycle, project=self.project, template=template, status='SUBMITTED')

    def history(self, year, value, kind='ACTUAL', metric='revenue_total', report='PL_TOTAL_WINE', month=0):
        batch = ManagementMetricBatch.objects.create(original_name='test.xlsx', sha256='a'*64,
            money_unit='YUAN', data_kind=kind, report_code=report, reason='测试', created_by=self.user)
        return ManagementMetricValue.objects.create(batch=batch, project=self.identity,metric=metric,
            year=year, month=month, value=value, source_sheet='收入',source_cell='N4')

    def budget(self, code, amount):
        return NormalizedValue.objects.create(upload=self.upload, report_code='PL_TOTAL_WINE', row_code=code,
            row_label=code, period='YEAR',data_year=2030,data_kind='BUDGET',month=None,unit='MONEY',value_int=amount)

    def result(self, **kwargs):
        return comparison_data(cycle=self.cycle,year=2030,report_code='PL_TOTAL_WINE',metric='revenue_total',**kwargs)

    def test_future_year_history_forecast_budget_and_delta(self):
        self.history(2027,100)
        self.history(2028,120)
        self.history(2029,150,'FORECAST')
        from budgeting.services.metrics import METRICS
        self.budget(METRICS['revenue_total']['rows']['PL_TOTAL_WINE'], 18000)
        row=self.result()['rows'][0]
        self.assertEqual(row['values'],[Decimal(100),Decimal(120),Decimal(150),Decimal(180)])
        self.assertEqual(row['delta'],30)
        self.assertEqual(row['growth'],20)

    def test_revision_is_immutable_and_explicit_blank_not_old_value(self):
        old=self.history(2027,100)
        self.history(2027,None)
        self.assertIsNone(self.result()['rows'][0]['values'][0])
        old.refresh_from_db()
        self.assertEqual(old.value,100)
        with self.assertRaises(ValueError):
            ManagementMetricValue.objects.filter(pk=old.pk).update(value=1)
        with self.assertRaises(ValueError):
            old.save()
        with self.assertRaises(ValueError):
            old.delete()

    def test_actual_can_be_comparison_base_without_forecast(self):
        self.history(2028,100)
        from budgeting.services.metrics import METRICS
        self.budget(METRICS['revenue_total']['rows']['PL_TOTAL_WINE'],12500)
        result=self.result(compare_index=1)
        self.assertEqual(result['rows'][0]['delta'],25)
        self.assertEqual(result['rows'][0]['growth'],25)
        self.assertEqual(result['totals']['delta'],25)
        self.assertEqual(result['comparison_label'],'2028 实际')

    def test_report_scopes_and_absent_year_do_not_mix(self):
        self.history(2027,100,report='PL_TOTAL_NOWINE')
        self.history(2029,200,'ACTUAL')
        self.assertEqual(self.result()['rows'][0]['values'],[None,None,None,None])

    def test_ratios_not_summed_or_averaged(self):
        self.history(2027,Decimal('.5'),metric='occ')
        other=IndicatorProject.objects.create(name='另一酒店')
        batch=ManagementMetricBatch.objects.last()
        ManagementMetricValue.objects.create(batch=batch,project=other,metric='occ',year=2027,month=0,
            value=Decimal('.9'),source_sheet='出租率',source_cell='N5')
        result=comparison_data(cycle=self.cycle,year=2030,report_code='PL_TOTAL_WINE',metric='occ')
        self.assertIsNone(result['totals']['values'][0])
        self.assertEqual([r['values'][0] for r in result['rows']],[Decimal('.5'),Decimal('.9')])

    def test_zero_and_missing_forecast_and_negative_growth(self):
        self.history(2029,0,'FORECAST')
        from budgeting.services.metrics import METRICS
        self.budget(METRICS['revenue_total']['rows']['PL_TOTAL_WINE'],100)
        row=self.result()['rows'][0]
        self.assertEqual(row['delta'],1)
        self.assertIsNone(row['growth'])

    def test_total_delta_not_computed_across_different_project_sets(self):
        self.history(2029,100,'FORECAST')
        other=IndicatorProject.objects.create(name='无预算酒店')
        batch=ManagementMetricBatch.objects.last()
        ManagementMetricValue.objects.create(batch=batch,project=other,metric='revenue_total',year=2029,month=0,
            value=100,source_sheet='收入',source_cell='N5')
        from budgeting.services.metrics import METRICS
        self.budget(METRICS['revenue_total']['rows']['PL_TOTAL_WINE'],12000)
        self.assertIsNone(self.result()['totals']['delta'])

    def test_confirm_rechecks_source_admin_revision_and_preserves_old(self):
        path=Path(self.temp.name)/'新底稿.xlsx'
        wb=Workbook();s=wb.active;s.title='收入（2027年）'
        s.append(['总收入']);s.append(['实际']);s.append(['项目']+[202700+m for m in range(1,13)]+['合计'])
        s.append(['测试']+[1]*12+[12]);wb.save(path)
        preview=preview_import(path,money_unit='YUAN')
        self.assertTrue(preview['valid'],preview['errors'])
        first=confirm_import(preview,source_path=path,user=self.user,reason='初始导入',expected_latest_id=0)
        self.assertEqual(first.values.count(),13)
        with self.assertRaisesMessage(ValueError,'其他管理员'):
            confirm_import(preview,source_path=path,user=self.user,reason='过期',expected_latest_id=0)
        second=confirm_import(preview,source_path=path,user=self.user,reason='有痕修订',expected_latest_id=first.pk)
        self.assertEqual(ManagementMetricBatch.objects.count(),2)
        self.assertTrue(Path(first.original.path).exists())
        self.assertGreater(second.pk,first.pk)
        project_user=User.objects.create_user(username='project',role='PROJECT',project=self.project)
        with self.assertRaises(PermissionError):
            confirm_import(preview,source_path=path,user=project_user,reason='无权',expected_latest_id=second.pk)
        s['B4']=2;wb.save(path)
        with self.assertRaisesMessage(ValueError,'原件'):
            confirm_import(preview,source_path=path,user=self.user,reason='被改',expected_latest_id=second.pk)
