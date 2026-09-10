import re
from decimal import Decimal

from budgeting.models import NormalizedValue, Project
from budgeting.services.budget_versions import selected_project_upload
from budgeting.services.special_indicators import comparison_data as imported_data
from budgeting.services.trends import _dimension


def _chinese(value):
    return ''.join(re.findall(r'[\u4e00-\u9fff]', value or ''))


SOURCES = {
    'BANQUET': ('B3宴会厅', '餐饮收入'),
    'RESTAURANT': ('B1餐厅汇总', '餐饮收入'),
    'RENT': ('PL_TOTAL_NOWINE', '租赁收入'),
}


def comparison_data(*, year, data_type, cycle=None, project_ids=None):
    result = imported_data(year=year, data_type=data_type, cycle=cycle, project_ids=project_ids)
    for row in result['rows']:
        row['source_message'] = '专项指标模板' if row['source'] else '尚未上传该期间数据'
        row['annual_complete'] = row['total'] is not None
    if data_type != 'BUDGET' or cycle is None or cycle.budget_year != year:
        return result

    projects = {p.pk: p for p in Project.objects.filter(is_active=True)}
    linked = {p['id']: projects.get(p['project_id']) for p in result['projects']}
    uploads = {key: selected_project_upload(project, cycle) if project else None
               for key, project in linked.items()}
    candidates = {}
    for key, upload in uploads.items():
        if upload:
            candidates[key] = list(NormalizedValue.objects.filter(
                upload=upload, report_code__in=[item[0] for item in SOURCES.values()],
                unit=NormalizedValue.Unit.MONEY))

    for row in result['rows']:
        if row['total'] is not None or any(value is not None for value in row['values']):
            continue
        upload = uploads.get(row['project_id'])
        if not upload:
            row['source_message'] = '该项目尚无本版本可读取的预算；可通过专项模板补充'
            continue
        spec = SOURCES.get(row['indicator'])
        if not spec:
            row['source_message'] = '未找到第三方能耗收入的独立对应科目，请通过专项模板补充预算'
            continue
        report_code, label = spec
        values = [v for v in candidates.get(row['project_id'], [])
                  if v.report_code == report_code and _chinese(v.row_label) == label
                  and _dimension(v)[:2] == (year, 'BUDGET')]
        if len({v.row_code for v in values}) > 1:
            row['source_message'] = '发现多个同名预算科目，无法唯一匹配，请通过专项模板确认'
            continue
        by_month = {}
        for value in values:
            month = _dimension(value)[2]
            if month in by_month:
                by_month = {}
                break
            by_month[month] = value
        if not by_month:
            row['source_message'] = f'未读取到{report_code}中的“{label}”，可通过专项模板补充'
            continue
        months = [by_month.get(month) for month in range(1, 13)]
        row['values'] = [float(Decimal(value.value_int) / 100) if value is not None else None for value in months]
        row['complete'] = all(value is not None for value in months)
        annual = by_month.get(None)
        total = Decimal(annual.value_int) / 100 if annual else (
            sum((Decimal(value.value_int) / 100 for value in months), Decimal(0)) if row['complete'] else None)
        row['total'] = float(total) if total is not None else None
        row['annual_complete'] = total is not None
        row['source'] = 'budget_workbook'
        row['source_message'] = f'{upload.original_name} · {report_code} · {label}'
        row['source_upload_id'] = str(upload.pk)
        row['source_cells'] = [v.source_cell for v in values]
        row['rehearsal'] = cycle.source_budget_year is not None
    result['coverage']['complete'] = sum(row['complete'] for row in result['rows'])
    return result
