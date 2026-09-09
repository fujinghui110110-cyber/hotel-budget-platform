from __future__ import annotations

from decimal import Decimal
import re
from urllib.parse import urlencode

from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from budgeting.cockpit_views import _cycle, admin_required
from budgeting.models import BudgetCycle, NormalizedValue, Project
from budgeting.services.report_labels import report_row_labels
from budgeting.services.version_reports import (
    VersionReportError,
    _period_label,
    _period_order,
    build_cycle_version_report_zip,
    build_project_version_report,
    cleanup_version_report,
    latest_upload_status,
    report_codes,
    report_title,
    selected_upload,
    usable_uploads,
)
from budgeting.services.workflow import RATIO_SCALE, audit


def _request_cycle(request) -> BudgetCycle:
    cycle = _cycle(request)
    if cycle is None:
        raise Http404("当前没有可用的预算版本。")
    return cycle


def _report_url(cycle: BudgetCycle, project: Project | None = None, report_code: str | None = None):
    params = {"cycle": cycle.pk}
    if project:
        params["project_id"] = project.pk
    if report_code:
        params["report_code"] = report_code
    return f"{reverse('management_version_report')}?{urlencode(params)}"


def _project_row(cycle: BudgetCycle, project: Project):
    upload = selected_upload(project, cycle)
    latest = latest_upload_status(project, cycle)
    from budgeting.services.data_read_audit import build_upload_audit_summary
    audit_summary = build_upload_audit_summary(latest) if latest and latest.status not in ("RECEIVED", "PROCESSING") else None
    if upload:
        state = "已上传，可查看/导出"
        state_class = "approved"
        if latest and latest.id != upload.id:
            state = f"最新上传为{latest.get_status_display()}；当前导出最近可用版本"
            state_class = "pending"
        return {
            "project": project,
            "audit_summary": audit_summary,
            "upload": upload,
            "report_count": len(report_codes(upload)),
            "state": state,
            "state_class": state_class,
            "report_url": _report_url(cycle, project),
            "download_url": f"{reverse('management_version_report_download', args=[project.pk])}?{urlencode({'cycle': cycle.pk})}",
        }
    if latest is None:
        state, state_class = "未上传", "pending"
    else:
        state, state_class = f"{latest.get_status_display()}，暂不可导出", "rejected"
    return {
        "project": project,
            "audit_summary": audit_summary,
        "upload": None,
        "report_count": 0,
        "state": state,
        "state_class": state_class,
        "report_url": None,
        "download_url": None,
    }


def _display_value(value):
    if value is None:
        return ""
    if value.unit == "MONEY":
        return f"{Decimal(value.value_int) / Decimal(100):,.2f}"
    if value.unit == "RATIO":
        if value.ratio_num is not None and value.ratio_den is not None:
            ratio = Decimal(value.ratio_num) / Decimal(value.ratio_den) if value.ratio_den else Decimal(0)
            return f"{ratio:.2%}"
        return f"{Decimal(value.value_int) / Decimal(RATIO_SCALE):.2%}"
    return f"{int(value.value_int):,d}"


def _is_annual_value(value):
    if value.month is not None:
        return False
    if value.data_year and value.data_kind:
        return True
    raw = (value.period or "").upper()
    return raw == "YEAR" or bool(re.fullmatch(r"[AFB]20\d{2}", raw))


@admin_required
def version_report(request):
    cycle = _request_cycle(request)
    projects, selected = usable_uploads(cycle)
    project_id = request.GET.get("project_id")
    selected_project = None
    selected_version = None
    selected_report_code = ""
    report_list = []
    rows = []
    periods = []
    period_labels = {}
    period_scope = request.GET.get("period_scope", "annual")
    if period_scope not in {"annual", "complete"}:
        period_scope = "annual"
    if project_id:
        if not project_id.isdigit():
            raise Http404("项目不存在。")
        selected_project = get_object_or_404(projects, pk=int(project_id))
        selected_version = selected.get(selected_project.pk)
        if selected_version:
            report_list = report_codes(selected_version)
            requested_code = request.GET.get("report_code", "")
            selected_report_code = requested_code if requested_code in report_list else (report_list[0] if report_list else "")
            if selected_report_code:
                values = list(
                    NormalizedValue.objects.filter(
                        upload=selected_version, report_code=selected_report_code
                    ).order_by("row_code", "period")
                )
                period_values = sorted(
                    {
                        value.period: value
                        for value in values
                        if period_scope == "complete" or _is_annual_value(value)
                    }.values(),
                    key=_period_order,
                )
                periods = [value.period for value in period_values]
                visible_values = [value for value in values if value.period in set(periods)]
                labels = report_row_labels(
                    cycle,
                    selected_report_code,
                    {value.row_code for value in visible_values},
                    current_labels=[(value.row_code, value.row_label) for value in visible_values],
                )
                value_map = {(value.row_code, value.period): value for value in visible_values}
                rows = [
                    {
                        "row_code": row_code,
                        "label": labels.get(row_code) or row_code,
                        "values": [
                            {"display": _display_value(value)} if value is not None else None
                            for value in (value_map.get((row_code, period)) for period in periods)
                        ],
                    }
                    for row_code in sorted({value.row_code for value in visible_values})
                ]
                period_labels = {value.period: _period_label(value, cycle.budget_year) for value in period_values}
    return render(
        request,
        "budgeting/version_report.html",
        {
            "cycle": cycle,
            "cycles": BudgetCycle.objects.order_by("-budget_year", "-revision_no"),
            "project_rows": [_project_row(cycle, project) for project in projects],
            "selected_project": selected_project,
            "selected_version": selected_version,
            "report_list": [{"code": code, "label": report_title(code)} for code in report_list],
            "selected_report_code": selected_report_code,
            "selected_report_title": report_title(selected_report_code) if selected_report_code else "",
            "rows": rows,
            "periods": periods if selected_project and selected_version and selected_report_code else [],
            "period_labels": period_labels,
            "period_scope": period_scope,
            "batch_url": f"{reverse('management_version_report_batch_download')}?{urlencode({'cycle': cycle.pk})}",
            "download_url": (
                f"{reverse('management_version_report_download', args=[selected_project.pk])}?{urlencode({'cycle': cycle.pk})}"
                if selected_project and selected_version
                else None
            ),
        },
    )


@admin_required
def version_report_download(request, project_id):
    cycle = _request_cycle(request)
    project = get_object_or_404(Project, pk=project_id, is_active=True)
    upload = selected_upload(project, cycle)
    if upload is None:
        return HttpResponse("该项目在本预算版本尚无可导出的已校验、已提交或已批准上传。", status=404, content_type="text/plain; charset=utf-8")
    try:
        output = build_project_version_report(upload)
    except VersionReportError as exc:
        return HttpResponse(str(exc), status=404, content_type="text/plain; charset=utf-8")
    audit(
        request.user,
        "VERSION_REPORT_DOWNLOADED",
        "UploadVersion",
        upload.id,
        {"report_count": len(report_codes(upload)), "export": "project_report"},
        project=project,
        cycle=cycle,
        upload=upload,
    )
    response = FileResponse(
        output.open("rb"),
        as_attachment=True,
        filename=f"{project.code}-{cycle.budget_year}-预算报表.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response._resource_closers.append(lambda: cleanup_version_report(output))
    return response


@admin_required
def version_report_batch_download(request):
    cycle = _request_cycle(request)
    try:
        archive = build_cycle_version_report_zip(cycle)
    except VersionReportError as exc:
        return HttpResponse(str(exc), status=404, content_type="text/plain; charset=utf-8")
    audit(
        request.user,
        "VERSION_REPORT_BATCH_DOWNLOADED",
        "BudgetCycle",
        cycle.pk,
        {
            "included_projects": len(archive.manifest["included_projects"]),
            "omitted_projects": len(archive.manifest["omitted_projects"]),
            "export": "all_project_reports",
        },
        cycle=cycle,
    )
    response = FileResponse(
        archive.path.open("rb"),
        as_attachment=True,
        filename=f"{cycle.budget_year}-全部项目预算报表.zip",
        content_type="application/zip",
    )
    response._resource_closers.append(lambda: cleanup_version_report(archive.path))
    return response
