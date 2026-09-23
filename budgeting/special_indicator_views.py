"""Year-aware special indicator comparison and staged, administrator-only imports."""
import json
import secrets
import time
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, OperationalError
from django.http import FileResponse, Http404, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

from budgeting.models import BudgetCycle, IndicatorProject, SpecialIndicatorBatch
from budgeting.services import special_indicators as service
from budgeting.services.special_indicator_comparison import comparison_data


def _admin(user):
    return user.is_superuser or user.role == 'ADMIN'


def _projects(user):
    projects = IndicatorProject.objects.all()
    return projects if _admin(user) else projects.filter(project_id=user.project_id) if user.project_id else projects.none()


def _year(value, default):
    try:
        value = int(value)
        return value if 1900 <= value <= 2200 else default
    except (ValueError, TypeError):
        return default


@login_required
def comparison(request):
    cycles = BudgetCycle.objects.order_by('-budget_year', '-revision_no')
    cycle_id = request.GET.get('cycle', '')
    if cycle_id and (not cycle_id.isascii() or not cycle_id.isdecimal() or len(cycle_id) > 10):
        raise Http404('预算版本不存在')
    cycle = get_object_or_404(cycles, pk=cycle_id) if cycle_id else cycles.first()
    year = cycle.budget_year if cycle else _year(request.GET.get('year'), timezone.now().year + 1)
    base = _year(request.GET.get('base_year'), year - 3)
    if base > year - 3:
        return HttpResponseBadRequest('历史起始年度须不晚于预算年度减三年，以保留两个历史实际年度和当年预测。')
    projects = _projects(request.user)
    selected = request.GET.get('project', '')
    if selected:
        if not selected.isascii() or not selected.isdecimal() or len(selected) > 10:
            raise Http404('项目不存在')
        project = get_object_or_404(projects, pk=selected)
        projects = projects.filter(pk=project.pk)
    periods = [(base, 'ACTUAL', f'{base} 实际'), (base + 1, 'ACTUAL', f'{base + 1} 实际'), (year - 1, 'FORECAST', f'{year - 1} 预测'), (year, 'BUDGET', f'{year} 预算')]
    datasets = []
    for period_year, data_type, label in periods:
        data = comparison_data(year=period_year, data_type=data_type, cycle=cycle if data_type == 'BUDGET' else None, project_ids=list(projects.values_list('pk', flat=True)))
        datasets.append({'label': label, 'data': data})
    batches = SpecialIndicatorBatch.objects.filter(active=True).order_by('-created_at')
    if not _admin(request.user):
        batches = batches.none()  # A batch original may contain other projects.
    return render(request, 'budgeting/special_indicators.html', {
        'cycle': cycle, 'cycles': cycles, 'projects': _projects(request.user), 'selected_project': selected,
        'base_year': base, 'max_base_year': year - 3, 'unit': request.GET.get('unit', 'YUAN'), 'datasets': datasets,
        'is_admin': _admin(request.user), 'batches': batches[:20],
    })


def _staging():
    path = Path(settings.BUDGET_STORAGE_ROOT) / 'special_indicators' / 'previews'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _replacement(preview):
    batches = SpecialIndicatorBatch.objects.filter(
        active=True, year=preview['year'], data_type=preview['data_type'], cycle_id=preview.get('cycle_id')
    ).order_by('pk')
    old_projects = sorted(set(batches.values_list('values__project__name', flat=True)) - {None})
    return {
        'batch_ids': list(batches.values_list('pk', flat=True)),
        'files': list(batches.values_list('original_name', flat=True)),
        'projects': old_projects,
        'omitted_projects': sorted(set(old_projects) - set(preview['projects'])),
    }


def _preview_context(preview):
    divisor = Decimal(10000) if preview['unit'] == 'WAN' else Decimal(1)
    groups = {}
    for row in preview['rows']:
        key = (row['project_name'], row['indicator'])
        group = groups.setdefault(key, {'project': key[0], 'indicator': service.INDICATORS.get(key[1], key[1]), 'values': [None] * 12, 'annual': None})
        value = Decimal(row['value']) / divisor if row['value'] is not None else None
        if row['month'] == 0:
            group['annual'] = value
        else:
            group['values'][row['month'] - 1] = value
    for group in groups.values():
        group['complete_months'] = sum(v is not None for v in group['values'])
        if group['annual'] is None and group['complete_months'] == 12:
            group['annual'] = sum(group['values'])
    return {
        'preview_groups': list(groups.values()),
        'valid_values': sum(row['value'] is not None for row in preview['rows']),
        'empty_values': sum(row['value'] is None for row in preview['rows']),
        'months': range(1, 13),
        'replacement': _replacement(preview),
    }


@login_required
def import_indicators(request):
    if not _admin(request.user):
        return HttpResponseForbidden('只有管理端可以导入专项指标。')
    context = {'cycles': BudgetCycle.objects.order_by('-budget_year', '-revision_no')}
    if request.method == 'POST':
        upload = request.FILES.get('file')
        if not upload or Path(upload.name).suffix.lower() != '.xlsx' or upload.size > 20 * 1024 * 1024:
            context['error'] = '请选择不超过 20 MB 的 .xlsx 指标模板。'
        else:
            year = _year(request.POST.get('year'), 0)
            kind, unit = request.POST.get('data_type'), request.POST.get('unit', 'YUAN')
            if not year or kind not in ('ACTUAL', 'FORECAST', 'BUDGET') or unit not in ('YUAN', 'WAN'):
                return HttpResponseBadRequest('年度、数据类型或单位无效。')
            cycle_id = request.POST.get('cycle', '')
            if kind == 'BUDGET' and (not cycle_id.isascii() or not cycle_id.isdecimal() or len(cycle_id) > 10):
                return HttpResponseBadRequest('请选择有效的预算版本。')
            cycle = get_object_or_404(BudgetCycle, pk=cycle_id) if kind == 'BUDGET' else None
            token = secrets.token_hex(24)
            source = _staging() / f'{token}.xlsx'
            with source.open('wb') as output:
                for chunk in upload.chunks():
                    output.write(chunk)
            try:
                preview = service.preview_import(source, year=year, data_type=kind, unit=unit, cycle=cycle)
            except (ValueError, OSError) as exc:
                source.unlink(missing_ok=True)
                context['error'] = str(exc)
            else:
                preview['original_name'] = Path(upload.name).name
                (_staging() / f'{token}.json').write_text(json.dumps({'preview': preview, 'created': time.time(), 'owner': request.user.pk, 'replacement': _replacement(preview)}, cls=DjangoJSONEncoder), encoding='utf-8')
                request.session['indicator_preview'] = token
                context.update(preview=preview, token=token, **_preview_context(preview))
    return render(request, 'budgeting/special_indicator_import.html', context)


@login_required
@require_POST
def confirm_indicators(request):
    if not _admin(request.user):
        return HttpResponseForbidden()
    token = request.POST.get('token', '')
    if len(token) != 48 or not all(c in '0123456789abcdef' for c in token) or request.session.get('indicator_preview') != token:
        return HttpResponseBadRequest('预览已失效，请重新上传。')
    meta_path, source = _staging() / f'{token}.json', _staging() / f'{token}.xlsx'
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return HttpResponseBadRequest('预览已失效，请重新上传。')
    if meta['owner'] != request.user.pk or time.time() - meta['created'] > 3600:
        return HttpResponseBadRequest('预览已过期，请重新上传。')
    preview = meta['preview']
    replacement = _replacement(preview)
    if replacement != meta.get('replacement', replacement):
        return HttpResponseBadRequest('当前批次已发生变化，请重新上传预览后确认覆盖范围。')
    if replacement['batch_ids'] and request.POST.get('ack_replace') != 'yes':
        return HttpResponseBadRequest('请确认整批替换及遗漏项目范围。')
    cycle = get_object_or_404(BudgetCycle, pk=preview['cycle_id']) if preview.get('cycle_id') else None
    fresh = service.preview_import(source, year=preview['year'], data_type=preview['data_type'], unit=preview['unit'], cycle=cycle)
    fresh['original_name'] = preview['original_name']
    if not fresh['valid'] or fresh['sha256'] != preview['sha256']:
        return HttpResponseBadRequest('源文件校验未通过，请重新上传。')
    claimed = meta_path.with_suffix('.claimed')
    try:
        meta_path.rename(claimed)
    except FileNotFoundError:
        return HttpResponseBadRequest('此预览已经确认，请勿重复提交。')
    try:
        service.confirm_import(fresh, source_path=source, user=request.user,
                               expected_active_batch_ids=meta['replacement']['batch_ids'])
    except (ValueError, PermissionError) as exc:
        claimed.rename(meta_path)
        return HttpResponseBadRequest(str(exc))
    except (IntegrityError, OperationalError):
        claimed.rename(meta_path)
        return HttpResponseBadRequest('其他操作正在更新数据，请重新预览后再确认。')
    claimed.unlink(missing_ok=True)
    request.session.pop('indicator_preview', None)
    meta_path.unlink(missing_ok=True)
    source.unlink(missing_ok=True)
    messages.success(request, '专项指标已导入，空白值保留为缺数。')
    return redirect('special_indicators')


@login_required
def template_download(request):
    path = Path(settings.BUDGET_STORAGE_ROOT) / 'special_indicators' / 'template.xlsx'
    if not path.is_file():
        raise Http404('指标模板尚未配置')
    return FileResponse(path.open('rb'), as_attachment=True, filename='专项指标上传模板.xlsx')


@login_required
def source_download(request, batch_id):
    if not _admin(request.user):
        return HttpResponseForbidden('原始文件包含多个项目，仅管理端可下载。')
    batch = get_object_or_404(SpecialIndicatorBatch, pk=batch_id)
    return FileResponse(batch.original.open('rb'), as_attachment=True, filename=batch.original_name)
