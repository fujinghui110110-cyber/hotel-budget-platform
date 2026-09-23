"""Versioned analytical history and current-round budget comparisons."""
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from django.core.files import File
from django.db import transaction
from django.db.models import Max

from budgeting.models import (IndicatorProject, ManagementMetricBatch, ManagementMetricValue,
                              NormalizedValue, Project, REPORTS)
from budgeting.services.metrics import METRICS
from budgeting.services.trends import latest_report_uploads, _aggregate

METRIC_CODES = ('revenue_total', 'revenue_fb', 'revenue_room', 'occ', 'adr', 'revpar',
                'profit_npi', 'revenue_banquet', 'revenue_seasonal', 'profit_gop',
                'cost_labor', 'cost_energy')
METRIC_CHOICES = [(code, '人工成本合计' if code == 'cost_labor' else METRICS[code]['label']) for code in METRIC_CODES]
NON_ADDITIVE = {'occ', 'adr', 'revpar'}
LABOR_COMPONENTS = ('cost_room_labor', 'cost_fb_labor', 'cost_other_labor', 'cost_backoffice_labor')


def latest_batch_id():
    return ManagementMetricBatch.objects.aggregate(value=Max('pk'))['value'] or 0


def _match_project(name):
    # Conservative aliases only; similar hotel names must not be silently merged.
    matches = list(Project.objects.filter(name=name)[:2])
    if not matches:
        matches = list(Project.objects.filter(code=name)[:2])
    if not matches:
        matches = list(Project.objects.filter(name=name + '酒店')[:2])
    return matches[0] if len(matches) == 1 else None


def preview_import(path, *, money_unit='WAN', data_kind='ACTUAL', report_code='PL_TOTAL_NOWINE'):
    from budgeting.services.management_metric_parser import parse_workbook
    result = parse_workbook(path, money_unit=money_unit, data_kind=data_kind)
    result['diagnostics'] = {'errors': result['errors'], 'warnings': result['warnings']}
    for key in ('errors', 'warnings'):
        issues = result[key]
        if key == 'warnings':
            placeholders = defaultdict(list)
            issues = []
            for item in result[key]:
                if isinstance(item, dict) and item.get('code') == 'PLACEHOLDER_VALUE':
                    placeholders[item.get('sheet', '')].append(item.get('cell') or item.get('source_cell', ''))
                else:
                    issues.append(item)
            for sheet, cells in placeholders.items():
                issues.append({'sheet': sheet, 'message': f'{len(cells)}个“-”占位值保留为缺数，不按0计算。'})
        result[key] = [
            ' '.join(str(item.get(field, '')) for field in ('sheet', 'cell', 'message')).strip()
            if isinstance(item, dict) else str(item) for item in issues
        ]
    result.update(money_unit=money_unit, data_kind=data_kind, report_code=report_code)
    if report_code not in REPORTS:
        result['errors'].append('请选择有效的四表报表口径。')
    if data_kind not in ('ACTUAL', 'FORECAST'):
        result['errors'].append('管理指标底稿仅用于历史实际或预测；预算从项目当前版本读取。')
    result['unlinked_projects'] = [name for name in result['projects'] if _match_project(name) is None]
    if result['unlinked_projects']:
        result['warnings'].append('以下名称尚未匹配预算项目，仅展示历史，不自动创建账号：' + '、'.join(result['unlinked_projects']))
    result['valid'] = not result['errors']
    return result


def confirm_import(preview, *, source_path, user, reason, expected_latest_id):
    if not user or not user.is_admin_role:
        raise PermissionError('仅管理员可确认历史指标。')
    if not reason or not reason.strip():
        raise ValueError('请填写导入或修订依据。')
    fresh = preview_import(source_path, money_unit=preview['money_unit'],
                           data_kind=preview['data_kind'], report_code=preview['report_code'])
    if not fresh['valid'] or fresh['sha256'] != preview['sha256']:
        raise ValueError('原件已改变或未通过校验，请重新预览。')
    batch = ManagementMetricBatch(original_name=Path(preview.get('original_name', Path(source_path).name)).name,
        sha256=fresh['sha256'], money_unit=fresh['money_unit'], data_kind=fresh['data_kind'],
        report_code=fresh['report_code'], reason=reason.strip(), created_by=user)
    try:
        with Path(source_path).open('rb') as handle:
            batch.original.save(batch.original_name, File(handle), save=False)
        with transaction.atomic():
            # Acquire the SQLite write lock before checking the observed revision.
            batch.save()
            prior = ManagementMetricBatch.objects.exclude(pk=batch.pk).aggregate(value=Max('pk'))['value'] or 0
            if int(expected_latest_id) != prior:
                raise ValueError('其他管理员已导入新批次，请重新预览后确认。')
            projects = {}
            for name in fresh['projects']:
                match = _match_project(name)
                identity = IndicatorProject.objects.filter(project=match).first() if match else None
                if identity is None:
                    identity, _ = IndicatorProject.objects.get_or_create(name=name)
                    if match and identity.project_id is None:
                        identity.project = match
                        identity.save(update_fields=['project'])
                projects[name] = identity
            ManagementMetricValue.objects.bulk_create([
                ManagementMetricValue(batch=batch, project=projects[r['project_name']], metric=r['metric'],
                    year=r['year'], month=r['month'], value=r['value'],
                    source_sheet=r['source_sheet'], source_cell=r['source_cell']) for r in fresh['rows']
            ])
        return batch
    except Exception:
        if batch.original.name:
            batch.original.delete(save=False)
        raise


def _delta(values, metric, compare_index=2):
    base, budget = values[compare_index], values[3]
    if base is None or budget is None:
        return None, None
    delta = budget - base
    # Loss/profit transitions do not have an unambiguous growth percentage.
    growth = None if base <= 0 or metric == 'occ' else delta / base * 100
    return delta, growth


def comparison_data(*, cycle, year, report_code, metric, month=0, base_year=None, project_id=None,
                     allowed_project_ids=None, compare_index=2):
    if metric not in METRIC_CODES or report_code not in REPORTS or not 0 <= month <= 12 or compare_index not in (0, 1, 2):
        raise ValueError('指标、报表或月份无效。')
    base_year = year - 3 if base_year is None else base_year
    periods = [(base_year, 'ACTUAL', f'{base_year} 实际'),
               (base_year + 1, 'ACTUAL', f'{base_year + 1} 实际'),
               (year - 1, 'FORECAST', f'{year - 1} 预测'), (year, 'BUDGET', f'{year} 预算')]
    if allowed_project_ids is not None:
        try:
            allowed_project_ids = {int(value) for value in allowed_project_ids if value is not None}
        except (TypeError, ValueError):
            allowed_project_ids = set()
    identities = list(IndicatorProject.objects.select_related('project').all())
    links = {p.pk: p.project or _match_project(p.name) for p in identities}
    # Existing budget projects must appear even before a historical import.
    uploads = list(latest_report_uploads(cycle)) if cycle and cycle.budget_year == year else []
    if allowed_project_ids is not None:
        identities = [p for p in identities if getattr(links.get(p.pk), 'pk', None) in allowed_project_ids]
        uploads = [pc for pc in uploads if pc.project_id in allowed_project_ids]
    linked_ids = {p.pk for p in links.values() if p is not None}
    for pc in uploads:
        if pc.project_id not in linked_ids:
            identities.append(IndicatorProject(pk=-pc.project_id, name=pc.project.name, project=pc.project))
            links[-pc.project_id] = pc.project
    if project_id is not None:
        identities = [p for p in identities if p.pk == project_id]
    values = ManagementMetricValue.objects.filter(metric=metric, batch__report_code=report_code,
        project_id__in=[p.pk for p in identities if p.pk > 0]).select_related('batch').order_by('-batch_id')
    history = {}
    for value in values:
        key = (value.project_id, value.year, value.batch.data_kind, value.month)
        history.setdefault(key, value)
    rows_by_upload = defaultdict(list)
    for value in NormalizedValue.objects.filter(upload_id__in=[pc.current_upload_id for pc in uploads],
                                                report_code=report_code).select_related('upload__cycle'):
        rows_by_upload[value.upload_id].append(value)
    pcs = {pc.project_id: pc for pc in uploads}
    notes = ['预算取所选轮次最新校验通过的上传，未上报不沿用旧轮次。',
             '历史只采用管理员确认的最新指标修订，空白保留缺数；原始批次保留。']
    if metric in NON_ADDITIVE:
        notes.append('出租率、房价与RevPAR不求和或简单平均；历史缺少房晚权重时，公司合计不计算。')
    if metric == 'revenue_seasonal':
        notes.append('当前预算季节性产品来源为B16月饼亭；历史含其他季节性产品时须先统一范围再解释差异。')
    if metric == 'cost_labor':
        notes.append('预算人工为客房、餐饮、其他经营部门及管理职能部门人工之和；任一组成项缺数不补0。')

    def budget(selected, m):
        if not selected:
            return None
        dimension = (year, 'BUDGET', m or None)
        components = LABOR_COMPONENTS if metric == 'cost_labor' else (metric,)
        buckets = [_aggregate(selected, rows_by_upload, METRICS[code], report_code, dimension) for code in components]
        if any(b['value'] is None or b['included_projects'] != len(selected) for b in buckets):
            return None
        return sum(Decimal(b['value']) for b in buckets) / (Decimal(10000) if metric == 'occ' else Decimal(100))

    def get_history(p, y, kind, m):
        row = history.get((p.pk, y, kind, m))
        if row is not None:
            return row.value
        # Derive annual sums only within one revision, never across partial revisions.
        monthly = [history.get((p.pk, y, kind, n)) for n in range(1, 13)]
        if m == 0 and metric not in NON_ADDITIVE and all(v is not None and v.value is not None for v in monthly):
            if len({v.batch_id for v in monthly}) == 1:
                return sum(v.value for v in monthly)
        return None

    def for_month(m):
        rows = []
        for p in identities:
            linked = links[p.pk]
            pc = pcs.get(linked.pk) if linked else None
            rowvalues = [get_history(p, y, kind, m) for y, kind, _ in periods[:3]] + [budget([pc], m) if pc else None]
            delta, growth = _delta(rowvalues, metric, compare_index)
            rows.append(dict(name=p.name, project_id=p.pk, values=rowvalues, delta=delta, growth=growth,
                             budget_linked=bool(linked)))
        totals = []
        coverage = []
        for index in range(4):
            available = [row['values'][index] for row in rows if row['values'][index] is not None]
            coverage.append(len(available))
            if metric in NON_ADDITIVE:
                if index == 3:
                    selected_ids = {getattr(links[p.pk], 'pk', None) for p in identities}
                    totals.append(budget([pc for pid, pc in pcs.items() if pid in selected_ids], m))
                else:
                    totals.append(available[0] if len(rows) == 1 and available else None)
            else:
                totals.append(sum(available) if available else None)
        # Total growth compares only identical project sets, avoiding coverage-induced growth.
        forecast_ids = {r['project_id'] for r in rows if r['values'][compare_index] is not None}
        budget_ids = {r['project_id'] for r in rows if r['values'][3] is not None}
        delta, growth = _delta(totals, metric, compare_index) if forecast_ids == budget_ids else (None, None)
        return rows, dict(values=totals,delta=delta,growth=growth,coverage=coverage,expected=len(rows))
    rows, totals = for_month(month)
    trend = [dict(month=m,label=f'{m}月',values=for_month(m)[1]['values']) for m in range(1,13)]
    return dict(rows=rows, totals=totals, periods=[label for _, _, label in periods],
                metric_label=dict(METRIC_CHOICES)[metric], unit='RATIO' if metric == 'occ' else 'MONEY',
                notes=notes, trend=trend, comparison_label=periods[compare_index][2])
