import hashlib
import json
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.db.models import Count, Max
from openpyxl.utils.cell import get_column_letter

from budgeting.models import NormalizedValue, REPORTS
from budgeting.services.legacy_rehearsal import _columns, _key, _number, _row_matches
from budgeting.services.workbook_reference import WorkbookReferenceError, read_workbook

SCHEMA_VERSION = 3
REASONS = {
    'REPORT_MISSING': '未找到汇总报表',
    'REPORT_EMPTY': '汇总报表没有数据',
    'MAPPING_MISSING': '未匹配到科目映射',
    'PERIOD_MISSING': '未识别到期间列',
    'CACHE_ERROR': '源单元格为错误值',
    'FORMULA_CACHE_MISSING': '公式没有可读取的数值缓存',
    'CELL_EMPTY': '源单元格为空',
    'NON_NUMERIC': '源单元格不是有效数值',
    'COUNT_NON_INTEGER': '数量科目含小数，未擅自取整',
    'NORMALIZED_MISSING': '源表有数值但系统未读取',
    'SOURCE_UNAVAILABLE': '来源文件无法读取',
    'MANIFEST_UNAVAILABLE': '映射清单无法读取',
}


def _path(value):
    path = Path(value)
    return path if path.is_absolute() else Path(settings.BASE_DIR) / path


def _stamp(path):
    try:
        stat = path.stat()
        return [str(path), stat.st_size, stat.st_mtime_ns]
    except OSError:
        return [str(path), None]


def _issue(code, report_code='', item=None, period='', sheet='', cell='', detail=''):
    item = item or {}
    return dict(report_code=report_code, row_code=item.get('row_code', ''),
                label=item.get('row_label', item.get('label', '')), period=period,
                source_sheet=sheet, source_cell=cell, reason_code=code,
                reason=REASONS[code], message=detail or REASONS[code])


def build_upload_audit(upload, *, refresh=False):
    storage = Path(settings.BUDGET_STORAGE_ROOT)
    source = Path(upload.recalculated_path or upload.original_path)
    if not source.is_absolute():
        source = storage / source
    template = upload.template or upload.cycle.template
    manifest_path = _path(template.manifest_path) if template else None
    base_template = upload.cycle.template or template
    base_path = _path(base_template.manifest_path) if base_template else None
    stats = NormalizedValue.objects.filter(upload=upload).aggregate(count=Count('pk'), latest=Max('pk'))
    fingerprint = hashlib.sha256(json.dumps([
        SCHEMA_VERSION, str(upload.pk), upload.sha256, upload.status, stats,
        _stamp(source), _stamp(manifest_path) if manifest_path else None,
        _stamp(base_path) if base_path else None,
    ], sort_keys=True).encode()).hexdigest()
    original = Path(upload.original_path)
    if not original.is_absolute():
        original = storage / original
    cache = original.parent / 'data_read_audit.json'
    if not refresh and upload.original_path:
        try:
            saved = json.loads(cache.read_text())
            if saved.get('fingerprint') == fingerprint:
                return saved
        except (OSError, ValueError):
            pass
    result = dict(schema_version=SCHEMA_VERSION, upload_id=str(upload.pk), fingerprint=fingerprint,
                  summary={}, issues=[])
    issues = result['issues']
    expected = read_count = report_count = 0
    try:
        manifest = json.loads(manifest_path.read_text()) if manifest_path else {}
        base = json.loads(base_path.read_text()) if base_path else {}
    except (OSError, ValueError) as exc:
        issues.append(_issue('MANIFEST_UNAVAILABLE', detail=f'无法读取该版本映射清单：{type(exc).__name__}'))
        manifest = base = {}
    try:
        workbook = read_workbook(source, include_cells=True)
    except (WorkbookReferenceError, OSError, ValueError) as exc:
        issues.append(_issue('SOURCE_UNAVAILABLE', detail=f'无法读取上传文件：{type(exc).__name__}'))
        workbook = None
    if workbook is not None:
        reports = dict(base.get('reports', {}))
        reports.update({k: v for k, v in manifest.get('reports', {}).items() if k not in reports})
        reports = {k: v for k, v in reports.items() if k in REPORTS}
        sheets = {_key(s['name']): s for s in workbook['sheets']}
        normalized = set(NormalizedValue.objects.filter(upload=upload).values_list('report_code', 'row_code', 'period'))
        dimensions = set(NormalizedValue.objects.filter(upload=upload).values_list('report_code', 'data_year', 'data_kind'))
        legacy = bool(manifest.get('legacy_rehearsal'))
        for report_code, report in reports.items():
            report_count += 1
            live_report = manifest.get('reports', {}).get(report_code, report)
            sheet_name = live_report.get('sheet', report.get('sheet', ''))
            sheet = sheets.get(_key(sheet_name))
            mapping = list(report.get('mapping', []))
            existing = {(m['row_code'], m.get('period')) for m in mapping}
            mapping.extend(m for m in live_report.get('mapping', []) if (m['row_code'], m.get('period')) not in existing)
            if sheet and report_code in base.get('reports', {}):
                for year, kind, title in [(upload.cycle.budget_year-3, 'ACTUAL', '实际'), (upload.cycle.budget_year-2, 'ACTUAL', '实际'), (upload.cycle.budget_year-1, 'FORECAST', '预测')]:
                    if (report_code, year, kind) not in dimensions:
                        issues.append(_issue('PERIOD_MISSING', report_code, period=f'{year}{title}', sheet=sheet_name, detail=f'系统未读取到{year}年{title}数据；请核对原表是否包含该年度及实际/预测标识，不能用其他期间替代。'))
            if not mapping:
                issues.append(_issue('MAPPING_MISSING', report_code, sheet=sheet_name, detail='该报表没有可用科目映射。'))
                continue
            if not sheet:
                expected += len(mapping)
                issues.append(_issue('REPORT_MISSING', report_code, sheet=sheet_name, detail=f'没有找到汇总报表“{sheet_name}”，{len(mapping)}个期望数据项无法读取。'))
                continue
            cells = sheet.get('cells', []) if sheet else []
            by_coord = {c['coordinate']: c for c in cells}
            source_rows = {}
            period_columns = {}
            if legacy and sheet:
                source_year = upload.cycle.source_budget_year or manifest.get('source_budget_year')
                parsed = _columns(cells, source_year, upload.cycle.budget_year-source_year)
                if parsed:
                    header, columns, label_limit = parsed
                    period_columns = {period: col for col, (period, year, kind) in columns.items()}
                    label_cells = [c for c in cells if c['row'] > header and c['column'] < label_limit and isinstance(c.get('cached_value'), str)]
                    canonical = list({m['row_code']: m for m in mapping}.values())
                    candidates = sorted({(c['row'], c['cached_value'].strip()) for c in label_cells})
                    source_rows = {m['row_code']: row for row, m in _row_matches(candidates, canonical).items()}
                for m in live_report.get('mapping', []):
                    if m.get('cell') in by_coord:
                        source_rows.setdefault(m['row_code'], by_coord[m['cell']]['row'])
            empty = bool(sheet) and not any(_number(c) is not None or c.get('is_formula') or c.get('error_status') for c in cells if c.get('row', 0) > (live_report.get('region', {}).get('header_row') or 0))
            if empty:
                expected += len(mapping)
                issues.append(_issue('REPORT_EMPTY', report_code, sheet=sheet_name, detail=f'汇总报表“{sheet_name}”没有可用数据，{len(mapping)}个期望数据项无法读取。'))
                continue
            for item in mapping:
                expected += 1
                period = 'YEAR' if item.get('period') in ('FY', 'YEAR') else item.get('period', '')
                if (report_code, item['row_code'], period) in normalized:
                    read_count += 1
                    continue
                coordinate = item.get('cell', '') if not legacy else ''
                reason = None
                if not sheet:
                    reason = 'REPORT_MISSING'
                elif empty:
                    reason = 'REPORT_EMPTY'
                elif legacy:
                    row = source_rows.get(item['row_code'])
                    column = period_columns.get(period)
                    if row is None:
                        reason = 'MAPPING_MISSING'
                    elif column is None:
                        reason = 'PERIOD_MISSING'
                    else:
                        coordinate = f'{get_column_letter(column)}{row}'
                cell = by_coord.get(coordinate, {})
                if reason is None:
                    if not coordinate:
                        reason = 'MAPPING_MISSING'
                    elif cell.get('error_status'):
                        reason = 'CACHE_ERROR'
                    elif cell.get('is_formula') and _number(cell) is None:
                        reason = 'FORMULA_CACHE_MISSING'
                    elif cell.get('cached_value') is None or cell.get('cached_value') == '':
                        reason = 'CELL_EMPTY'
                    elif _number(cell) is None:
                        reason = 'NON_NUMERIC'
                    elif item.get('unit') == 'COUNT' and _number(cell) != _number(cell).to_integral_value():
                        reason = 'COUNT_NON_INTEGER'
                    else:
                        reason = 'NORMALIZED_MISSING'
                detail = REASONS[reason]
                if reason == 'CACHE_ERROR':
                    detail += f"：{cell.get('cached_value') or cell.get('error_status')}"
                issues.append(_issue(reason, report_code, item, period, sheet_name, coordinate, detail))
    result['summary'] = dict(issue_count=len(issues), report_count=report_count, expected_cells=expected,
                             read_cells=read_count, missing_cells=expected-read_count,
                             reason_counts=dict(Counter(i['reason_code'] for i in issues)))
    try:
        if upload.original_path and source.is_file():
            cache.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass
    return result


def build_upload_audit_summary(upload):
    return build_upload_audit(upload)['summary']
