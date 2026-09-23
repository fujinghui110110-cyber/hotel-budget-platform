"""HTTP presentation for the four controlled reports."""
from dataclasses import replace
from io import BytesIO
import re
from urllib.parse import urlencode

from django.http import Http404, HttpResponse
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import render
from openpyxl import Workbook

from budgeting.models import REPORTS, Project, PlanProject, NormalizedValue
from .report_context import ReportContext
from .report_query import query_report, query_report_table


MODE_LABELS = {"WORKING": "工作稿", "APPROVED": "正式稿", "FROZEN": "冻结快照"}


def request_context(request, cycle, report_code, project=None):
    project_ids = tuple(PlanProject.objects.filter(plan_id=cycle.plan_id).values_list("project_id", flat=True)) if cycle.plan_id else tuple(Project.objects.filter(is_active=True).values_list("pk", flat=True))
    if project:
        if project.pk not in project_ids:
            raise ValueError("项目不属于本年度报表集合。")
        project_ids = (project.pk,)
    elif request.GET.get("projects"):
        requested = tuple(int(p) for p in request.GET["projects"].split(","))
        if not set(requested).issubset(project_ids):
            raise ValueError("项目不属于本年度报表集合。")
        project_ids = requested
    return ReportContext(int(request.GET.get("year", cycle.budget_year)), cycle.pk, report_code,
                         request.GET.get("source_mode", "WORKING"), project_ids,
                         snapshot_id=request.GET.get("snapshot") or None)


def _label(period, year):
    if period == "YEAR":
        return f"{year}年预算全年"
    if period.isdigit():
        return f"{year}年预算{int(period)}月"
    match = re.fullmatch(r"([AF])(\d{4})(?:M(\d{2}))?", period)
    return f"{match[2]}年{'实际' if match[1] == 'A' else '预测'}{str(int(match[3]))+'月' if match[3] else '全年'}"



def _unavailable_report(request, cycle, report_code, context, *, drilldown=False):
    """A broken mapping is a visible data gap, never a licence to guess totals."""
    if request.GET.get('download'):
        return HttpResponse('模板或冻结来源文件不可读取，无法导出。',status=400)
    query=context.query_string()
    common={'cycle':cycle,'report_code':report_code,'report_name':REPORTS[report_code],
            'report_scope':'数据暂不可用：模板或冻结来源文件缺失',
            'project_scope_query':'?'+query,'source_mode':context.source_mode,
            'data_unavailable':'模板或冻结来源文件缺失，未读取或推测任何数据。'}
    if drilldown:
        common.update(row_code=request.GET.get('row_code',''),row_name=request.GET.get('row_code',''),
                      period=request.GET.get('period','YEAR'),period_label=request.GET.get('period','YEAR'),
                      contributions=[],total='—',total_amount='—',unallocated='—',unallocated_amount='—')
        return render(request,'budgeting/management_report_drilldown.html',common)
    common.update(values={},units={},rows=[],row_codes=[],periods=[],period_labels={},period_options=[],
                  period_scope='budget',is_summary=True,REPORTS=REPORTS)
    return render(request,'budgeting/management_report.html',common)


def render_report(request, cycle, report_code, project=None):
    try:
        context = request_context(request, cycle, report_code, project)
        table = query_report_table(context, include_history=True)
    except OSError:
        return _unavailable_report(request,cycle,report_code,context)
    except (ValueError, ObjectDoesNotExist) as exc:
        return HttpResponse(str(exc), status=400)
    scope = request.GET.get("period_scope", "budget")
    options = [("t3_actual",f"T-3 实际 · {cycle.budget_year-3}"), ("t2_actual",f"T-2 实际 · {cycle.budget_year-2}"),
               ("t1_forecast",f"T-1 预测 · {cycle.budget_year-1}"), ("budget",f"T 预算 · {cycle.budget_year}"), ("all","全部期间")]
    if scope not in dict(options):
        scope = "budget"
    prefixes = {"t3_actual":f"A{cycle.budget_year-3}", "t2_actual":f"A{cycle.budget_year-2}", "t1_forecast":f"F{cycle.budget_year-1}"}
    periods = [p for p in table if scope == "all" or (scope == "budget" and (p == "YEAR" or p.isdigit())) or (scope in prefixes and p.startswith(prefixes[scope]))]
    values, units = {}, {}
    for period, result in table.items():
        for metric in result.metrics:
            values[metric.row_code,period] = None if metric.value is None else metric.value * (100 if metric.unit == "MONEY" else 10000 if metric.unit == "RATIO" else 1)
            units[metric.row_code,period] = metric.unit
    annual = table['YEAR']
    incomplete_rows = {metric.row_code for period in periods for metric in table[period].metrics if metric.missing}
    partial = any(table[p].missing_projects for p in periods) or bool(incomplete_rows)
    query = context.query_string() + '&' + urlencode({'period_scope':scope, **({'project_id':project.pk} if project else {})})
    if request.GET.get('download') == 'xlsx':
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '预算报表'
        sheet.append([REPORTS[report_code], MODE_LABELS[context.source_mode], cycle.budget_year, cycle.revision_no])
        sheet.append(['覆盖项目', annual.coverage['available'], '应报项目', annual.coverage['expected']])
        sheet.append(['科目', *[_label(p, cycle.budget_year) for p in periods]])
        lookup = {p:{m.row_code:m for m in table[p].metrics} for p in periods}
        for metric in annual.metrics:
            sheet.append([metric.label + ('（已上报合计，不完整）' if metric.row_code in incomplete_rows else ''), *[lookup[p][metric.row_code].value for p in periods]])
        provenance = workbook.create_sheet('数据来源')
        provenance.append(['上下文',context.query_string()])
        provenance.append(['数据集合指纹',annual.identity])
        provenance.append(['项目','上传版本','缺报原因'])
        for pid in context.project_ids:
            provenance.append([annual.project_names[pid],annual.upload_ids.get(pid),annual.missing_projects.get(pid)])
        provenance.append(['期间','科目','项目','缺失原因'])
        for period in periods:
            for metric in table[period].metrics:
                for pid,reason in metric.missing.items():
                    provenance.append([_label(period,cycle.budget_year),metric.label,annual.project_names[pid],reason])
        buffer = BytesIO(); workbook.save(buffer)
        response = HttpResponse(buffer.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="budget-report.xlsx"'
        return response
    return render(request, 'budgeting/management_report.html', {
        'report_code':report_code,'report_name':REPORTS[report_code], 'REPORTS':REPORTS,
        'row_codes':[{'code':m.row_code,'label':m.label + ('（不完整）' if m.row_code in incomplete_rows else '')} for m in annual.metrics],
        'first_row_code':annual.metrics[0].row_code if annual.metrics else '', 'first_period':periods[0] if periods else '',
        'periods':periods, 'period_labels':{p:_label(p, cycle.budget_year) for p in periods},
        'period_options':[{'value':v,'label':l} for v,l in options], 'period_scope':scope,
        'cycle':cycle,'values':values,'units':units, 'is_summary':True,'selected_project':project,
        'report_scope':MODE_LABELS[context.source_mode] + (' · 已上报合计（非完整年度总数）' if partial else ' · 完整项目合计'), 'source_mode':context.source_mode,
        'snapshot_id':context.snapshot_id or '', 'project_ids_query':','.join(map(str,context.project_ids)), 'project_scope_query':'?'+query,
        'coverage':annual.coverage, 'missing_projects':[{'name':annual.project_names[p], 'reason':r} for p,r in annual.missing_projects.items()],
    })


def render_drilldown(request, cycle, report_code, project=None):
    try:
        context = request_context(request, cycle, report_code, project)
        period = request.GET.get('period') or 'YEAR'
        match = re.fullmatch(r'([AF])(\d{4})(?:M(\d{2}))?',period)
        if match:
            context = replace(context,data_year=int(match[2]),data_kind='ACTUAL' if match[1]=='A' else 'FORECAST',period=match[3] or 'YEAR')
        else:
            context = replace(context,period=period)
        result = query_report(context)
    except OSError:
        return _unavailable_report(request,cycle,report_code,context,drilldown=True)
    except (ValueError, ObjectDoesNotExist) as exc:
        return HttpResponse(str(exc),status=400)
    row = request.GET.get('row_code') or request.GET.get('row')
    metric = next((m for m in result.metrics if m.row_code == row),None)
    if metric is None:
        raise Http404
    def display(value):
        return '—' if value is None else f'{value:.2%}' if metric.unit=='RATIO' else f'{value:,.0f}' if metric.unit=='COUNT' else f'{value:,.2f}'
    # Provenance is one batch, using the same exact selected uploads and dimensions.
    sources = {v.upload_id:v for v in NormalizedValue.objects.filter(upload_id__in=result.upload_ids.values(),report_code=report_code,row_code=row,
        data_year=context.data_year or context.budget_year,data_kind=context.data_kind,month=None if context.period=='YEAR' else int(context.period))}
    contributions=[]
    for pid in context.project_ids:
        source=next((v for uid,v in sources.items() if str(uid)==result.upload_ids.get(pid)),None)
        contributions.append({'project_name':result.project_names[pid], 'version':result.upload_ids.get(pid,'—'),
            'amount':display(metric.by_project.get(pid)), 'sheet':source.source_sheet if source else '',
            'cell':source.source_cell if source else '', 'formula':metric.missing.get(pid) or (source.source_formula if source else '')})
    query=context.query_string() + ('&'+urlencode({'project_id':project.pk}) if project else '')
    return render(request,'budgeting/management_report_drilldown.html',{'report_code':report_code,'report_name':REPORTS[report_code],
        'row_code':row,'row_name':metric.label + ('（已上报合计，不完整）' if metric.missing else ''),'period':period,'period_label':_label(period,cycle.budget_year),'contributions':contributions,
        'total':display(metric.value),'total_amount':display(metric.value),'unallocated':'—','unallocated_amount':'—',
        'project_scope_query':'?'+query})
