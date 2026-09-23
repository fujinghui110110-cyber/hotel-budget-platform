"""Read historical source caches for an explicitly configured rehearsal cycle."""
import json
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings
from budgeting.services.template_paths import resolve_template_path
from django.db import transaction

from budgeting.excel.extract import sheet_slug
from budgeting.models import NormalizedValue, REPORTS, TemplateVersion, UploadVersion, ValidationIssue
from budgeting.services.workbook_reference import read_workbook, WorkbookReferenceError


def _key(value):
    return re.sub(r'[\s%％()（）\-—、:：]+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _number(cell):
    value = cell.get('cached_value') if cell else None
    if (
        value is None
        or isinstance(value, bool)
        or cell.get('error_status')
        or cell.get('is_error')
    ):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _unit(label, cell):
    if re.search(r'率|占比|OCC|%', label, re.I) or '%' in cell.get('number_format', ''):
        return 'RATIO'
    if re.search(r'房间数|房晚|房数|可卖房|占用房|免费房|自用房|已售房|人数|天数|人次|数量', label):
        return 'COUNT'
    return 'MONEY'


def _is_blank_input(cell):
    """Return whether a source cell is an ordinary, non-formula blank."""
    if (
        not cell
        or cell.get('is_formula')
        or cell.get('formula')
        or cell.get('error_status')
        or cell.get('is_error')
    ):
        return False
    raw = cell.get('cached_value')
    return raw is None or (isinstance(raw, str) and not raw.strip())


def _column_name(column):
    letters = ''
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _blank_cell(row, column, sheet):
    """Make a sparse blank cell while retaining its workbook coordinate."""
    return {
        'row': row,
        'column': column,
        'coordinate': f'{_column_name(column)}{row}',
        'cached_value': None,
        'display_value': None,
        'formula': None,
        'is_formula': False,
        'error_status': None,
        'is_error': False,
        '_sheet': sheet,
    }


def _value(upload, code, row_code, label, period, year, kind, cell, unit, *, blank_as_zero=False):
    number = _number(cell)
    if number is None:
        if not blank_as_zero or not _is_blank_input(cell):
            return None
        number = Decimal(0)
    base = dict(upload=upload, report_code=code, row_code=row_code, row_label=label[:240], period=period,
                data_year=year, data_kind=kind, month=int(period) if re.fullmatch(r'\d{2}', period) else None,
                source_sheet=cell['_sheet'], source_cell=cell['coordinate'], source_formula=cell.get('formula') or '', unit=unit)
    if unit == 'RATIO':
        return NormalizedValue(**base, value_int=0, ratio_num=int((number * 10**8).quantize(Decimal(1), rounding=ROUND_HALF_UP)), ratio_den=10**8)
    scale = 100 if unit == 'MONEY' else 1
    return NormalizedValue(**base, value_int=int((number * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def _columns(cells, source_year, offset):
    """Use an actual 12-month header, never guess columns from position."""
    candidates = defaultdict(dict)
    for c in cells:
        v = str(c.get('cached_value') or '').strip()
        m = re.fullmatch(r'(?:(20\d{2})年)?(0?[1-9]|1[0-2])月?', v)
        if not m:
            m = re.fullmatch(r'(20\d{2})(0[1-9]|1[0-2])', v)
        if m and (not m[1] or int(m[1]) == source_year):
            candidates[c['row']][int(m[2])] = c['column']
    headers = [(r, m) for r, m in candidates.items() if len(m) == 12]
    if not headers:
        return None
    header, months = min(headers)
    columns = {col: (f'{month:02d}', source_year+offset, 'BUDGET') for month, col in months.items()}
    for c in cells:
        if not header-1 <= c['row'] <= header+3:
            continue
        v = str(c.get('cached_value') or '').strip()
        if re.fullmatch(r'全年合计|全年|全年预算|年合计|年度合计|本年预算', v):
            columns[c['column']] = ('YEAR', source_year+offset, 'BUDGET')
        m = re.fullmatch(r'(20\d{2})\s*年?\s*(实际|预测|预估|预算)', v)
        if m:
            year = int(m[1]) + offset
            kind = {'实际':'ACTUAL', '预测':'FORECAST', '预估':'FORECAST', '预算':'BUDGET'}[m[2]]
            prefix = {'ACTUAL':'A', 'FORECAST':'F', 'BUDGET':'B'}[kind]
            columns[c['column']] = ('YEAR' if int(m[1]) == source_year and kind == 'BUDGET' else f'{prefix}{year}', year, kind)
    unique = {}
    for col, period in sorted(columns.items()):
        if period not in unique.values():
            unique[col] = period
    return header, unique, min(months.values())


def _row_matches(rows, canonical):
    """Order-preserving exact label matching avoids shifts from inserted details."""
    result = {}
    cursor = 0
    for source_row, label in rows:
        matches = [i for i in range(cursor, len(canonical)) if _key(canonical[i]['row_label']) == _key(label)]
        if matches:
            i = matches[0]
            result[source_row] = canonical[i]
            cursor = i+1
    return result


@transaction.atomic
def process_legacy_rehearsal(upload, source_path, run):
    source_year = upload.cycle.source_budget_year
    if not source_year:
        raise ValueError('历史原表只能用于明确配置的演练版本')
    def issue(code, message, severity='P2', location=''):
        ValidationIssue.objects.create(run=run, code=code, message=message, severity=severity, location=location[:200])
    try:
        source = read_workbook(source_path, include_cells=True)
    except (WorkbookReferenceError, OSError, ValueError) as exc:
        issue('LEGACY_INVALID_PACKAGE', str(exc), 'P0')
        upload.status = UploadVersion.Status.REJECTED
        upload.save(update_fields=['status'])
        return False
    template_path = resolve_template_path(upload.cycle.template.manifest_path)
    if not template_path.is_absolute():
        template_path = Path(settings.BASE_DIR) / template_path
    canonical_manifest = json.loads(template_path.read_text())
    offset = upload.cycle.budget_year - source_year
    title_years = {int(m[1]) for sheet in source['sheets'] for c in sheet.get('cells', [])
                   if isinstance(c.get('cached_value'), str)
                   for m in [re.search(r'(20\d{2})\s*年[^\n]{0,12}预算', c['cached_value'])] if m}
    if title_years and source_year not in title_years:
        issue('LEGACY_SOURCE_YEAR_MISMATCH', f'原表预算标题年度为{sorted(title_years)}，与演练原表年度{source_year}不符。', 'P0')
    source_names = {_key(s['name']): s for s in source['sheets']}
    aliases = {_key(label): code for code, label in REPORTS.items()}
    # Source convention places the space before the opening parenthesis.
    matched = {code for name, code in aliases.items() if name in source_names}
    manifest = {'legacy_rehearsal': True, 'source_budget_year': source_year, 'budget_year': upload.cycle.budget_year,
                'year_offset': offset, 'reports': {}, 'sheets': [], 'source_sha256': upload.sha256}
    values = []
    skipped = []
    error_count = 0
    missing_formula_count = 0
    for sheet in source['sheets']:
        name = sheet['name']
        cells = sheet.get('cells', [])
        for cell in cells:
            cell['_sheet'] = name
        manifest['sheets'].append({'name': name})
        code = aliases.get(_key(name), sheet_slug(name))
        grid = _columns(cells, source_year, offset)
        if not grid:
            skipped.append(name)
            continue
        header, columns, first_month = grid
        by_row = defaultdict(dict)
        for cell in cells:
            by_row[cell['row']][cell['column']] = cell
        label_scores = defaultdict(int)
        for cell in cells:
            if cell['row'] > header and cell['column'] not in columns and isinstance(cell.get('cached_value'), str) and re.search(r'[一-鿿]', cell['cached_value']):
                label_scores[cell['column']] += 1
        if not label_scores:
            skipped.append(name)
            continue
        label_col = max(label_scores, key=lambda col:(label_scores[col], col))
        explicit = [c['column'] for c in cells if header <= c['row'] <= header+3 and _key(c.get('cached_value')) in {'项目','description项目','科目','费用项目'}]
        if explicit:
            label_col = explicit[0]
        source_rows = [(r, str(cols[label_col]['cached_value']).strip()) for r, cols in sorted(by_row.items())
                       if r > header and label_col in cols and isinstance(cols[label_col].get('cached_value'), str)]
        canonical = []
        seen = set()
        for item in canonical_manifest.get('reports', {}).get(code, {}).get('mapping', []):
            if item['row_code'] not in seen:
                canonical.append(item)
                seen.add(item['row_code'])
        matches = _row_matches(source_rows, canonical)
        mapping = []
        for r, label in source_rows:
            meta = matches.get(r)
            row_code = meta['row_code'] if meta else f'S{r:04d}'
            actual_label = meta['row_label'] if meta else label
            for col, (period, year, kind) in columns.items():
                cell = by_row[r].get(col)
                budget_blank = bool(meta and kind == 'BUDGET' and year == upload.cycle.budget_year)
                if cell is None and budget_blank:
                    cell = _blank_cell(r, col, name)
                if not cell:
                    continue
                unit = meta['unit'] if meta else _unit(label, cell)
                if cell.get('error_status') or cell.get('is_error'):
                    error_count += 1
                    continue
                if (cell.get('is_formula') or cell.get('formula')) and _number(cell) is None:
                    missing_formula_count += 1
                value = _value(upload, code, row_code, actual_label, period, year, kind, cell, unit,
                               blank_as_zero=budget_blank)
                if value is not None:
                    values.append(value)
                    if kind == 'BUDGET' and year == upload.cycle.budget_year:
                        mapping.append({'row_code':row_code, 'row_label':actual_label, 'period':'FY' if period=='YEAR' else period,
                                        'cell':cell['coordinate'], 'formula':cell.get('formula') or '', 'unit':unit,
                                        'aggregation': meta.get('aggregation','SUM') if meta else 'SUM'})
        manifest['reports'][code] = {'sheet':name, 'mapping':mapping, 'region':{'header_row':header}}
        if code in REPORTS:
            missing = [item['row_label'] for item in canonical if item['row_code'] not in {m['row_code'] for m in matches.values()}]
            if missing:
                issue('LEGACY_UNMATCHED_ROWS', '以下标准科目未按名称匹配，源行按原名称保留：'+'、'.join(missing), location=name)
    for code in REPORTS:
        if code not in matched:
            issue('LEGACY_MISSING_REPORT', f'原文件没有 {REPORTS[code]}，不复制其他报表代替。')
    if skipped:
        issue('LEGACY_NON_GRID_SHEETS', '以下工作表未识别完整12个月表头，完整内容保留在原件导出：'+'、'.join(skipped))
    issue('LEGACY_CACHED_VALUES', f'演练数据：读取{source_year}年原表已保存的数值；按+{offset}年映射。未重算、未执行宏、未刷新外链，不代表真实{upload.cycle.budget_year}年预算。')
    if error_count or missing_formula_count:
        issue('LEGACY_SOURCE_CACHE_GAPS', f'识别范围内原表有{error_count}个错误值、{missing_formula_count}个无数值的公式；相关错误或公式值保持缺失，未按零处理。')
    if not any(v.report_code in REPORTS and v.period == 'YEAR' for v in values):
        issue('LEGACY_NO_SUMMARY', '未识别到可用年度损益汇总表，不能形成预算报表。', 'P0')
    NormalizedValue.objects.filter(upload=upload).delete()
    NormalizedValue.objects.bulk_create(values, batch_size=500)
    from budgeting.excel.supplementary import extract_supplementary_values
    extract_supplementary_values(upload, source_path, validation_run=run)
    path = source_path.parent / 'legacy_manifest.json'
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    template, _ = TemplateVersion.objects.update_or_create(version=f'LEGACY-{upload.pk}', defaults={
        'budget_year':upload.cycle.budget_year, 'file_path':str(source_path), 'manifest_path':str(path),
        'formula_manifest_hash':upload.sha256, 'rule_version':'LEGACY-CACHE-1', 'is_active':False})
    upload.template = template
    run.passed = not run.issues.filter(severity='P0').exists()
    run.save(update_fields=['passed'])
    upload.status = UploadVersion.Status.VALIDATED if run.passed else UploadVersion.Status.REJECTED
    upload.save(update_fields=['template','status'])
    return run.passed
