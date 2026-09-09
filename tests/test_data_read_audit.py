import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from budgeting.models import BudgetCycle, NormalizedValue, Project, TemplateVersion, UploadVersion
from budgeting.services.data_read_audit import build_upload_audit


class DataReadAuditTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.xlsx'
        self.source.write_bytes(b'source')
        self.manifest = self.root / 'manifest.json'
        self.mapping = [dict(row_code=f'R{i}', row_label=f'科目{i}', period='FY', cell=f'B{i}', unit='MONEY') for i in range(1, 5)]
        self.manifest.write_text(json.dumps({'reports': {'PL_TOTAL_WINE': {'sheet': '汇总', 'mapping': self.mapping}}}))
        self.template = TemplateVersion.objects.create(version='AUDIT', budget_year=2027, file_path=str(self.source), manifest_path=str(self.manifest), formula_manifest_hash='a'*64)
        self.project = Project.objects.create(code='AUDIT', name='项目')
        self.cycle = BudgetCycle.objects.create(name='预算', budget_year=2027, template=self.template)
        self.upload = UploadVersion.objects.create(project=self.project, cycle=self.cycle, template=self.template, original_path=str(self.source), sha256='a'*64)

    def audit(self, cells=None, sheets=None, refresh=True):
        workbook = {'sheets': sheets if sheets is not None else [{'name': '汇总', 'cells': cells or []}]}
        with patch('budgeting.services.data_read_audit.read_workbook', return_value=workbook):
            result = build_upload_audit(self.upload, refresh=refresh)
            result['issues'] = [i for i in result['issues'] if not (i['reason_code'] == 'PERIOD_MISSING' and not i['row_code'])]
            return result

    def cell(self, row, value, **kwargs):
        return dict(coordinate=f'B{row}', row=row, column=2, cached_value=value, **kwargs)

    def test_zero_is_read_and_source_zero_not_imported_is_missing(self):
        NormalizedValue.objects.create(upload=self.upload, report_code='PL_TOTAL_WINE', row_code='R1', period='YEAR', unit='MONEY', value_int=0, source_sheet='汇总', source_cell='B1')
        result = self.audit([self.cell(1, 0), self.cell(2, 0)])
        self.assertEqual(result['summary']['read_cells'], 1)
        self.assertEqual(result['issues'][0]['row_code'], 'R2')
        self.assertEqual(result['issues'][0]['reason_code'], 'NORMALIZED_MISSING')

    def test_cache_error_formula_without_cache_and_blank_are_distinct(self):
        result = self.audit([self.cell(1, '#REF!', error_status='#REF!'), self.cell(2, None, is_formula=True), self.cell(3, None), self.cell(4, 1)])
        self.assertEqual([i['reason_code'] for i in result['issues']], ['CACHE_ERROR', 'FORMULA_CACHE_MISSING', 'CELL_EMPTY', 'NORMALIZED_MISSING'])

    def test_missing_report_and_empty_report_are_distinct(self):
        missing = self.audit(sheets=[])
        self.assertEqual(missing['issues'][0]['reason_code'], 'REPORT_MISSING')
        self.assertEqual(len(missing['issues']), 1)
        self.assertEqual(missing['summary']['missing_cells'], 0)
        self.assertEqual(missing['summary']['expected_cells'], 0)
        self.assertEqual(self.audit()['issues'][0]['reason_code'], 'REPORT_EMPTY')

    def test_cache_reused_and_invalidated_after_normalization(self):
        self.audit([self.cell(1, 0)])
        with patch('budgeting.services.data_read_audit.read_workbook') as reader:
            build_upload_audit(self.upload)
            reader.assert_not_called()
        NormalizedValue.objects.create(upload=self.upload, report_code='PL_TOTAL_WINE', row_code='R1', period='YEAR', unit='MONEY', value_int=0, source_sheet='汇总', source_cell='B1')
        self.assertEqual(self.audit([self.cell(1, 0)], refresh=False)['summary']['read_cells'], 1)

    def test_legacy_manifest_missing_mapping_uses_base_expected_rows(self):
        self.cycle.source_budget_year = 2026
        self.cycle.save()
        legacy = self.root / 'legacy.json'
        legacy.write_text(json.dumps({'legacy_rehearsal': True, 'reports': {'PL_TOTAL_WINE': {'sheet': '汇总', 'mapping': []}}}))
        self.upload.template = TemplateVersion.objects.create(version='LEGACY-AUDIT', budget_year=2027, file_path=str(self.source), manifest_path=str(legacy), formula_manifest_hash='a'*64)
        self.upload.save()
        cells = [dict(coordinate=f'{chr(66+i)}1', row=1, column=i+2, cached_value=f'{i+1:02d}') for i in range(12)]
        cells += [dict(coordinate='A2', row=2, column=1, cached_value='不存在的科目'), self.cell(2, 100)]
        result = self.audit(cells)
        self.assertEqual(result['summary']['expected_cells'], 4)
        self.assertEqual(result['summary']['reason_counts']['MAPPING_MISSING'], 4)

    def test_missing_historical_forecast_periods_are_explicit(self):
        with patch('budgeting.services.data_read_audit.read_workbook', return_value={'sheets': [{'name': '汇总', 'cells': [self.cell(1, 0)]}]}):
            result = build_upload_audit(self.upload, refresh=True)
        periods = {i['period'] for i in result['issues'] if i['reason_code'] == 'PERIOD_MISSING'}
        self.assertEqual(periods, {'2024实际', '2025实际', '2026预测'})

    def test_missing_sheet_without_mapping_has_only_one_notice(self):
        self.manifest.write_text(json.dumps({'reports': {'PL_TOTAL_WINE': {'sheet': '缺失表', 'mapping': []}}}))
        with patch('budgeting.services.data_read_audit.read_workbook', return_value={'sheets': []}):
            result = build_upload_audit(self.upload, refresh=True)
        self.assertEqual([i['reason_code'] for i in result['issues']], ['REPORT_MISSING'])
        self.assertEqual(result['summary']['missing_cells'], 0)
