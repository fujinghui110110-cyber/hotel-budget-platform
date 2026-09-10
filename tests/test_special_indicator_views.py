from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from budgeting.models import BudgetCycle, IndicatorProject, Project, User


class SpecialIndicatorViewsTests(TestCase):
    def setUp(self):
        self.cycle = BudgetCycle.objects.create(name='测试版', budget_year=2030)
        self.project = Project.objects.create(code='P1', name='本项目')
        self.own = IndicatorProject.objects.create(name='本项目', project=self.project)
        self.other = IndicatorProject.objects.create(name='其他项目')
        self.admin = User.objects.create_user(username='indicator_admin', role='ADMIN')
        self.user = User.objects.create_user(username='indicator_project', role='PROJECT', project=self.project)

    @patch('budgeting.special_indicator_views.comparison_data')
    def test_project_scope_and_dynamic_years(self, comparison):
        comparison.return_value = {'projects': [], 'indicators': {}, 'rows': [], 'coverage': {}}
        self.client.force_login(self.user)
        response = self.client.get(reverse('special_indicators'), {'cycle': self.cycle.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([call.kwargs['year'] for call in comparison.call_args_list], [2027, 2028, 2029, 2030])
        self.assertTrue(all(call.kwargs['project_ids'] == [self.own.pk] for call in comparison.call_args_list))
        self.assertNotContains(response, '其他项目')
        self.assertEqual(self.client.get(reverse('special_indicators'), {'project': self.other.pk}).status_code, 404)

    def test_import_is_admin_only(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('special_indicator_import')).status_code, 403)
        self.assertEqual(self.client.post(reverse('special_indicator_confirm'), {'token': 'a' * 48}).status_code, 403)
        self.assertEqual(self.client.get(reverse('special_indicator_source', args=[1])).status_code, 403)

    def test_invalid_and_replayed_preview_rejected(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(reverse('special_indicator_confirm'), {'token': '../x'}).status_code, 400)
        self.assertEqual(self.client.post(reverse('special_indicator_confirm'), {'token': 'a' * 48}).status_code, 400)
        self.assertEqual(self.client.get(reverse('special_indicator_confirm')).status_code, 405)
        response = self.client.get(reverse('special_indicator_import'))
        self.assertContains(response, '元（默认）')
        self.assertContains(response, '万元')

    @patch('budgeting.special_indicator_views.service.confirm_import')
    @patch('budgeting.special_indicator_views.service.preview_import')
    def test_staged_import_confirm_once(self, preview_import, confirm_import):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        preview_import.return_value = {'valid': True, 'errors': [], 'warnings': [], 'projects': ['本项目'], 'rows': [], 'year': 2024, 'data_type': 'ACTUAL', 'unit': 'YUAN', 'cycle_id': None, 'sha256': 'verified'}
        self.client.force_login(self.admin)
        with tempfile.TemporaryDirectory() as root, override_settings(BUDGET_STORAGE_ROOT=root):
            response = self.client.post(reverse('special_indicator_import'), {'year': '2024', 'data_type': 'ACTUAL', 'unit': 'YUAN', 'file': SimpleUploadedFile('source.xlsx', b'test')})
            self.assertEqual(response.status_code, 200)
            token = self.client.session['indicator_preview']
            response = self.client.post(reverse('special_indicator_confirm'), {'token': token})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(confirm_import.call_count, 1)
            self.assertEqual(self.client.post(reverse('special_indicator_confirm'), {'token': token}).status_code, 400)

    def test_preview_counts_and_wan_display(self):
        from budgeting.special_indicator_views import _preview_context
        preview = {'year': 2024, 'data_type': 'ACTUAL', 'cycle_id': None, 'unit': 'WAN', 'projects': ['本项目'], 'rows': [{'project_name': '本项目', 'indicator': 'BANQUET', 'month': m, 'value': '10000' if m == 1 else None} for m in range(13)]}
        context = _preview_context(preview)
        self.assertEqual(context['valid_values'], 1)
        self.assertEqual(context['empty_values'], 12)
        self.assertEqual(context['preview_groups'][0]['values'][0], 1)
        self.assertIsNone(context['preview_groups'][0]['annual'])
        self.assertEqual(context['preview_groups'][0]['complete_months'], 1)

    @patch('budgeting.special_indicator_views.service.preview_import')
    def test_whole_batch_replacement_requires_acknowledgement(self, preview_import):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from budgeting.models import SpecialIndicatorBatch, SpecialIndicatorValue
        old = SpecialIndicatorBatch.objects.create(year=2024, data_type='ACTUAL', unit='YUAN', original_name='旧批次.xlsx', sha256='old')
        SpecialIndicatorValue.objects.create(batch=old, project=self.other, indicator='BANQUET', month=1, value=12)
        preview_import.return_value = {'valid': True, 'errors': [], 'warnings': [], 'projects': ['本项目'], 'rows': [], 'year': 2024, 'data_type': 'ACTUAL', 'unit': 'YUAN', 'cycle_id': None, 'sha256': 'verified'}
        self.client.force_login(self.admin)
        with tempfile.TemporaryDirectory() as root, override_settings(BUDGET_STORAGE_ROOT=root):
            response = self.client.post(reverse('special_indicator_import'), {'year': '2024', 'data_type': 'ACTUAL', 'unit': 'YUAN', 'file': SimpleUploadedFile('source.xlsx', b'test')})
            self.assertContains(response, '旧批次.xlsx')
            self.assertContains(response, '本次遗漏 1 个原项目：其他项目')
            self.assertContains(response, '0 个有效数值')
            token = self.client.session['indicator_preview']
            response = self.client.post(reverse('special_indicator_confirm'), {'token': token})
            self.assertEqual(response.status_code, 400)

    def test_malformed_filter_ids_do_not_raise_server_error(self):
        self.client.force_login(self.admin)
        for key in ('cycle', 'project'):
            response = self.client.get(reverse('special_indicators'), {key: 'not-an-id'})
            self.assertEqual(response.status_code, 404)
        response = self.client.get(reverse('special_indicators'), {'cycle': self.cycle.pk, 'base_year': 2030})
        self.assertEqual(response.status_code, 400)
