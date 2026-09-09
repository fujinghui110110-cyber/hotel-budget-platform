import math
import re
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Count, F, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from budgeting.excel.money import cents_to_yuan
from budgeting.forms import (
    ProjectAccountForm,
    ProjectForm,
    RejectForm,
    ResetPasswordForm,
    UploadForm,
)
from budgeting.models import (
    AdjustmentBatch,
    AdjustmentLine,
    AuditEvent,
    BudgetCycle,
    FreezeSnapshot,
    NormalizedValue,
    ProcessingJob,
    Project,
    ProjectCycle,
    REPORTS,
    TemplateVersion,
    UploadVersion,
    ValidationIssue,
)
from budgeting.services import (
    active_cycle,
    approve_upload,
    create_driver_adjustment,
    create_full_adjustment,
    freeze_cycle,
    issue_adjustment,
    preview_adjustment,
    process_upload_now,
    project_value_details,
    reject_upload,
    reopen_cycle,
    save_upload,
    submit_upload,
)
from budgeting.services.drivers import DRIVERS, driver_value_label, format_cascade_value, simulate_driver
from budgeting.services.pnl_graph import display_value, edit_value, parse_edit
from budgeting.services.workflow import (
    RATIO_SCALE,
    _company_value_details,
    _round_ratio,
    company_trend,
    freeze_preconditions,
    project_trend_rows,
    sub_table_reports,
)
from budgeting.services.template_delivery import signed_template_copy
from budgeting.services.report_labels import report_row_labels


HISTORY_PERIOD_RE = re.compile(r"^[AF]\d{4}$")
BUDGET_MONTH_RE = re.compile(r"^(0[1-9]|1[0-2])$")
TREND_COLORS = [
    "#0f766e", "#b45309", "#4f46e5", "#be123c", "#15803d",
    "#7c3aed", "#0369a1", "#a16207", "#475569", "#0891b2",
]

DRIVER_REPORT = "PL_TOTAL_WINE"
DRIVER_OPTIONS = [
    {"code": "OCC", "label": "出租率", "unit": "%"},
    {"code": "ADR", "label": "平均房价（单价）", "unit": "元"},
    {"code": "ROOMS", "label": "房间数", "unit": "间"},
    {"code": "ROOM_REV", "label": "客房收入", "unit": "万元"},
]


def role_required(role):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            is_admin = request.user.role == "ADMIN" or request.user.is_superuser
            if role == "ADMIN" and not is_admin:
                raise Http404()
            if role == "PROJECT" and request.user.role != "PROJECT":
                raise Http404()
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


@login_required
def home(request):
    is_admin = request.user.role == "ADMIN" or request.user.is_superuser
    return redirect("planning_overview" if is_admin else "project_home")


@role_required("PROJECT")
def project_home(request):
    cycle = active_cycle()
    uploads = UploadVersion.objects.filter(project=request.user.project, cycle=cycle) if cycle else []
    batches = []
    open_lines = []
    if cycle:
        batches = AdjustmentBatch.objects.filter(
            cycle=cycle, lines__project=request.user.project
        ).distinct()
        open_lines = list(
            AdjustmentLine.objects.filter(
                cycle=cycle, project=request.user.project, status=AdjustmentLine.Status.OPEN
            ).select_related("batch")
        )
    return render(request, "budgeting/project_dashboard.html", {
        "cycle": cycle,
        "uploads": uploads,
        "adjustment_batches": batches,
        "open_lines": open_lines,
    })


@role_required("PROJECT")
def template_download(request):
    cycle = active_cycle()
    template = cycle.template if cycle and cycle.template_id else TemplateVersion.objects.filter(is_active=True).order_by("-created_at").first()
    if not cycle or not template:
        raise Http404("当前没有可下载模板")
    path = signed_template_copy(template, request.user.project, cycle)
    return FileResponse(path.open("rb"), as_attachment=True, filename=Path(template.file_path).name)


@role_required("PROJECT")
def upload_new(request):
    cycle = active_cycle()
    if request.method == "POST":
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                upload = save_upload(request.user.project, cycle, form.cleaned_data["file"])
                job = process_upload_now(upload) if settings.BUDGET_PROCESS_UPLOAD_INLINE else ProcessingJob.objects.get(upload=upload)
                if job.status == ProcessingJob.Status.FAILED:
                    messages.warning(request, "文件已保存，但处理失败，请在详情页查看问题明细。")
                elif job.status in (ProcessingJob.Status.QUEUED, ProcessingJob.Status.RUNNING):
                    messages.info(request, "文件已上传，校验任务正在运行，请稍后查看详情。")
                elif upload.status == UploadVersion.Status.REJECTED:
                    messages.warning(request, "文件已保存，但校验未通过，请在详情页查看问题明细。")
                else:
                    messages.success(request, "文件已上传并完成校验。")
                return redirect("project_upload_detail", upload.id)
            except ValueError as exc:
                form.add_error("file", str(exc))
    else:
        form = UploadForm()
    return render(request, "budgeting/project_upload_new.html", {"form": form, "cycle": cycle})


@login_required
def project_upload_detail(request, upload_id):
    upload = get_object_or_404(UploadVersion, id=upload_id)
    is_admin = request.user.role == "ADMIN" or request.user.is_superuser
    if not is_admin and upload.project_id != request.user.project_id:
        return HttpResponse(status=403)
    issues = ValidationIssue.objects.filter(run__upload=upload).order_by("severity", "code")
    job = ProcessingJob.objects.filter(upload=upload).order_by("-created_at").first()
    return render(request, "budgeting/project_upload_detail.html", {
        "upload": upload,
        "issues": issues,
        "job": job,
        "is_admin": is_admin,
        "reject_form": RejectForm(),
    })


@role_required("PROJECT")
def project_submit_upload(request, upload_id):
    upload = get_object_or_404(UploadVersion, id=upload_id, project=request.user.project)
    if request.method == "POST":
        submit_upload(upload, request.user)
    return redirect("project_upload_detail", upload.id)


@role_required("PROJECT")
def project_history(request):
    cycle = active_cycle()
    uploads = UploadVersion.objects.filter(project=request.user.project, cycle=cycle) if cycle else []
    return render(request, "budgeting/project_history.html", {"uploads": uploads, "cycle": cycle})


@role_required("PROJECT")
def project_adjustments(request):
    cycle = active_cycle()
    batches = []
    if cycle:
        batches = list(
            AdjustmentBatch.objects.filter(cycle=cycle)
            .filter(
                Q(project=request.user.project)
                | Q(project__isnull=True, lines__project=request.user.project)
            )
            .prefetch_related(
                Prefetch(
                    "lines",
                    queryset=AdjustmentLine.objects.filter(project=request.user.project).select_related("project"),
                )
            )
            .distinct()
            .order_by("-created_at")
        )
    return render(
        request,
        "budgeting/project_adjustments.html",
        {"adjustment_batches": batches, "batches": batches, "cycle": cycle},
    )


@role_required("ADMIN")
def management_cycles(request):
    cycles = list(BudgetCycle.objects.order_by("-budget_year", "-revision_no"))
    latest_per_year = {}
    for c in cycles:
        latest_per_year.setdefault(c.budget_year, c.revision_no)
    return render(request, "budgeting/management_cycles.html", {
        "cycles": cycles,
        "active_cycle": active_cycle(),
        "next_openable_ids": [c.id for c in cycles if c.revision_no == latest_per_year[c.budget_year]],
    })


@role_required("ADMIN")
def management_cycle_reopen(request, cycle_id):
    if request.method != "POST":
        return redirect("management_cycles")
    cycle = get_object_or_404(BudgetCycle, id=cycle_id)
    try:
        reopen_cycle(
            cycle,
            request.user,
            project_ids=list(ProjectCycle.objects.filter(cycle=cycle).values_list("project_id", flat=True)),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("management_cycles")
    messages.success(request, f"已开放下一修订版：{cycle.name} R{cycle.revision_no + 1}（{cycle.budget_year} 年度）。")
    return redirect("management_cycles")


@role_required("ADMIN")
def management_projects(request):
    cycle = active_cycle()
    rows = []
    submitted_count = 0
    if cycle:
        for project in Project.objects.filter(is_active=True).order_by("code"):
            latest = UploadVersion.objects.filter(project=project, cycle=cycle).order_by("-created_at").first()
            pc = ProjectCycle.objects.filter(project=project, cycle=cycle).select_related("current_upload").first()
            rows.append({
                "project": project,
                "latest": latest,
                "current": pc.current_upload if pc else None,
            })
        submitted_count = UploadVersion.objects.filter(cycle=cycle, status=UploadVersion.Status.SUBMITTED).count()
    return render(request, "budgeting/management_projects.html", {
        "cycle": cycle,
        "rows": rows,
        "submitted_count": submitted_count,
        "project_count": len(rows),
    })


@role_required("ADMIN")
def management_dashboard(request):
    cycle = active_cycle()
    report_code = request.GET.get("report") or "PL_TOTAL_WINE"
    if report_code not in REPORTS:
        report_code = "PL_TOTAL_WINE"
    kpis, projects = _dashboard_data(cycle, report_code)
    project_total = ProjectCycle.objects.filter(cycle=cycle).count() if cycle else 0
    return render(request, "budgeting/management_dashboard.html", {
        "cycle": cycle,
        "report_code": report_code,
        "report_name": REPORTS[report_code],
        "kpis": kpis,
        "projects": projects,
        "project_total": project_total,
        "approved_total": len(projects),
        "REPORTS": REPORTS,
        "sub_tables": sub_table_reports(cycle),
    })


@role_required("ADMIN")
def management_report(request, report_code):
    project = _requested_report_project(request)
    return _render_report(request, report_code, project)


@role_required("PROJECT")
def project_report(request, report_code):
    if not request.user.project_id:
        raise Http404
    return _render_report(request, report_code, request.user.project)


def _requested_report_project(request):
    project_id = request.GET.get("project_id")
    if not project_id:
        return None
    if not project_id.isdigit():
        raise Http404
    return get_object_or_404(Project, pk=project_id)


def _render_report(request, report_code, project=None):
    cycle_id = request.GET.get("cycle")
    if cycle_id:
        if not cycle_id.isdigit():
            raise Http404
        cycle = get_object_or_404(BudgetCycle, pk=cycle_id)
    else:
        cycle = active_cycle()
    details = project_value_details(project, cycle, report_code) if project else (_company_value_details(cycle, report_code) if cycle else {})
    values = {key: detail["value_int"] for key, detail in details.items()}
    units = {key: detail["unit"] for key, detail in details.items()}
    codes = sorted({row_code for row_code, _ in values})
    labels = {}
    if cycle:
        current_ids = ProjectCycle.objects.filter(
            cycle=cycle, project__is_active=True, current_upload__cycle=cycle,
            current_upload__project=F("project"), current_upload__status=UploadVersion.Status.APPROVED,
        )
        if project:
            current_ids = current_ids.filter(project=project)
        labels = {
            rc: lbl
            for rc, lbl in NormalizedValue.objects.filter(
                upload_id__in=current_ids.values("current_upload_id"), report_code=report_code
            )
            .values_list("row_code", "row_label")
            .distinct()
            if rc in codes
        }
    labels = report_row_labels(cycle, report_code, codes, current_labels=labels)
    row_codes = [{"code": rc, "label": labels.get(rc) or rc} for rc in codes]
    budget_year = cycle.budget_year if cycle else 0
    period_scope = request.GET.get("period_scope") or "budget"
    allowed_scopes = {"t3_actual", "t2_actual", "t1_forecast", "budget", "all"}
    if period_scope not in allowed_scopes:
        period_scope = "budget"

    def period_dimension(period):
        value = str(period).strip().upper()
        match = re.fullmatch(r"([AFB])(20\d{2})(?:M(0[1-9]|1[0-2]))?", value)
        if match:
            kind = {"A": "ACTUAL", "F": "FORECAST", "B": "BUDGET"}[match.group(1)]
            return int(match.group(2)), kind, int(match.group(3)) if match.group(3) else None
        if BUDGET_MONTH_RE.fullmatch(value):
            return budget_year, "BUDGET", int(value)
        if value == "YEAR":
            return budget_year, "BUDGET", None
        return None

    scope_dimensions = {
        "t3_actual": (budget_year - 3, "ACTUAL"),
        "t2_actual": (budget_year - 2, "ACTUAL"),
        "t1_forecast": (budget_year - 1, "FORECAST"),
        "budget": (budget_year, "BUDGET"),
    }

    def period_sort_key(period):
        dimension = period_dimension(period)
        if not dimension:
            return (1, str(period))
        year, kind, month = dimension
        kind_order = {"ACTUAL": 0, "FORECAST": 1, "BUDGET": 2}.get(kind, 9)
        return (0, year, kind_order, month if month is not None else 13, str(period))

    def period_label(period):
        dimension = period_dimension(period)
        if not dimension:
            return str(period)
        year, kind, month = dimension
        kind_label = {"ACTUAL": "实际", "FORECAST": "预测", "BUDGET": "预算"}[kind]
        suffix = f"{month}月" if month is not None else "全年"
        return f"{year}年{kind_label}{suffix}"

    all_periods = sorted({period for _, period in values}, key=period_sort_key)
    selected_dimension = scope_dimensions.get(period_scope)
    periods = [
        period for period in all_periods
        if period_scope == "all" or (
            period_dimension(period)
            and period_dimension(period)[:2] == selected_dimension
        )
    ]
    period_labels = {period: period_label(period) for period in periods}
    period_options = [
        {"value": "t3_actual", "label": f"T-3 实际 · {budget_year - 3}"},
        {"value": "t2_actual", "label": f"T-2 实际 · {budget_year - 2}"},
        {"value": "t1_forecast", "label": f"T-1 预测 · {budget_year - 1}"},
        {"value": "budget", "label": f"T 预算 · {budget_year}"},
        {"value": "all", "label": "全部期间"},
    ]
    rows = [
        {
            "code": row_code,
            "row_code": row_code,
            "name": labels.get(row_code) or row_code,
            "values": [
                _display_company_detail(details.get((row_code, period)))
                for period in periods
            ],
        }
        for row_code in codes
    ]
    query_parts = []
    if project:
        query_parts.append(f"project_id={project.pk}")
    if cycle:
        query_parts.append(f"cycle={cycle.pk}")
    query_parts.append(f"period_scope={period_scope}")
    return render(request, "budgeting/management_report.html", {
        "report_code": report_code,
        "report_name": _report_display_name(cycle, report_code),
        "row_codes": row_codes,
        "first_row_code": row_codes[0]["code"] if row_codes else "",
        "first_period": periods[0] if periods else "",
        "periods": periods,
        "period_labels": period_labels,
        "period_options": period_options,
        "period_scope": period_scope,
        "cycle": cycle,
        "values": values,
        "units": units,
        "rows": rows,
        "REPORTS": REPORTS,
        "is_summary": report_code in REPORTS,
        "selected_project": project,
        "report_scope": project.name if project else "公司正式汇总",
        "project_scope_query": "?" + "&".join(query_parts),
    })


@login_required
def report_catalog(request):
    if request.user.role == "ADMIN" or request.user.is_superuser:
        project = _requested_report_project(request)
        projects = Project.objects.filter(is_active=True).order_by("code")
    elif request.user.role == "PROJECT" and request.user.project_id:
        project, projects = request.user.project, []
    else:
        raise Http404
    cycle = active_cycle()
    tables = sub_table_reports(cycle)
    if project:
        ids = ProjectCycle.objects.filter(
            cycle=cycle, project=project, current_upload__cycle=cycle,
            current_upload__project=project, current_upload__status=UploadVersion.Status.APPROVED,
        ).values("current_upload_id")
        counts = dict(NormalizedValue.objects.filter(upload_id__in=ids).values("report_code")
                      .annotate(n=Count("row_code", distinct=True)).values_list("report_code", "n"))
        tables = [dict(table, rows=counts.get(table["code"], 0)) for table in tables]
    return render(request, "budgeting/report_catalog.html", {
        "cycle": cycle, "selected_project": project, "projects": projects,
        "REPORTS": REPORTS, "sub_tables": tables,
    })


@role_required("ADMIN")
def management_report_drilldown(request, report_code):
    cycle = active_cycle()
    selected_project = _requested_report_project(request)
    row_code = request.GET.get("row_code") or request.GET.get("row") or ""
    period = request.GET.get("period") or ""
    current_ids = []
    if cycle:
        current_ids = list(ProjectCycle.objects.filter(
            cycle=cycle,
            current_upload__cycle=cycle,
            current_upload__project=F("project"),
            current_upload__status=UploadVersion.Status.APPROVED,
            project__is_active=True,
        ).values_list("current_upload_id", flat=True))
    contributions = []
    total = f"{cents_to_yuan(0):,.2f}"
    zero = f"{cents_to_yuan(0):,.2f}"
    if row_code and current_ids:
        qs = NormalizedValue.objects.filter(
            upload_id__in=current_ids,
            report_code=report_code,
            row_code=row_code,
        )
        if selected_project:
            qs = qs.filter(upload__project=selected_project)
        if period:
            qs = qs.filter(period=period)
        agg = {}
        for value in qs.select_related("upload", "upload__project").order_by("upload__project__code", "period"):
            pid = value.upload.project_id
            a = agg.setdefault(pid, {"unit": value.unit, "value_int": 0, "ratio_num": 0, "ratio_den": 0,
                                     "project": value.upload.project, "upload_id": str(value.upload_id),
                                     "source_sheet": value.source_sheet, "source_cell": value.source_cell,
                                     "source_formula": value.source_formula})
            _aggregate_row(a, value.unit, value.value_int, value.ratio_num, value.ratio_den)
        for pid, a in agg.items():
            display = _cell_display(a["unit"], a["value_int"], a["ratio_num"], a["ratio_den"])
            contributions.append({
                "project": a["project"].code,
                "project_name": f"{a['project'].code} {a['project'].name}",
                "account": a["project"].code,
                "version": a["upload_id"],
                "row_code": row_code,
                "period": period or "全年",
                "budget": display,
                "forecast": display,
                "amount": display,
                "value": display,
                "source_sheet": a["source_sheet"],
                "source_cell": a["source_cell"],
                "source_formula": a["source_formula"],
                "sheet": a["source_sheet"],
                "cell": a["source_cell"],
                "formula": a["source_formula"],
                "status": "passed",
                "status_label": "已纳入",
                "value_int": a["value_int"],
                "ratio_num": a["ratio_num"],
                "ratio_den": a["ratio_den"],
                "unit": a["unit"],
            })
        if contributions:
            units = {c["unit"] for c in contributions}
            if units == {NormalizedValue.Unit.RATIO}:
                rn = sum(c["ratio_num"] for c in contributions)
                rd = sum(c["ratio_den"] for c in contributions)
                total = _cell_display(NormalizedValue.Unit.RATIO, 0, rn, rd)
                zero = total
            else:
                tv = sum(c["value_int"] for c in contributions)
                total = f"{cents_to_yuan(tv):,.2f}"
                zero = f"{cents_to_yuan(0):,.2f}"
    row_name = row_code
    if cycle and row_code:
        label = NormalizedValue.objects.filter(
            upload__cycle=cycle, report_code=report_code, row_code=row_code
        ).values_list("row_label", flat=True).first()
        if label:
            row_name = label
    row_name = report_row_labels(cycle, report_code, [row_code],
                                 current_labels={row_code: row_name}).get(row_code, row_code)
    return render(request, "budgeting/management_report_drilldown.html", {
        "report_code": report_code,
        "report_name": _report_display_name(cycle, report_code),
        "row_code": row_code,
        "row_name": row_name,
        "period": period,
        "period_label": period or "全年",
        "values": [],
        "contributions": contributions,
        "total": total,
        "total_amount": total,
        "unallocated": zero,
        "unallocated_amount": zero,
        "project_scope_query": f"?project_id={selected_project.pk}" if selected_project else "",
    })


@role_required("ADMIN")
def management_trend(request):
    cycle = active_cycle()
    report_code = request.GET.get("report_code") or request.GET.get("report") or "PL_TOTAL_NOWINE"
    if report_code not in REPORTS:
        report_code = "PL_TOTAL_NOWINE"
    view_mode = "project" if request.GET.get("view") == "project" else "company"
    options = _kpi_row_options(cycle, report_code)
    row_code = request.GET.get("row_code") or ""
    valid_codes = {o["code"] for o in options}
    if row_code not in valid_codes:
        row_code = _default_trend_row(options)

    budget_year = cycle.budget_year if cycle else 0
    unit = NormalizedValue.Unit.MONEY
    selected_label = row_code
    annual_labels, monthly_labels = [], []
    annual_series, monthly_series = [], []
    annual_svg, monthly_svg = "", ""

    if row_code and cycle:
        company_details = company_trend(cycle, report_code, row_code)
        selected_label = next((o["label"] for o in options if o["code"] == row_code), row_code)
        if company_details:
            unit = next(iter(company_details.values()))["unit"]
        if view_mode == "project":
            rows = project_trend_rows(cycle, report_code, row_code)
            all_periods = set()
            for r in rows:
                all_periods.update(r["series"])
            annual, monthly = _split_periods(all_periods)
            annual_labels = _annual_labels(annual, budget_year)
            monthly_labels = _monthly_labels(monthly)
            annual_series = [
                {"name": f"{r['code']} {r['name']}", "color": TREND_COLORS[i % len(TREND_COLORS)],
                 "values": _series_values(r["series"], annual)}
                for i, r in enumerate(rows)
            ]
            monthly_series = [
                {"name": f"{r['code']} {r['name']}", "color": TREND_COLORS[i % len(TREND_COLORS)],
                 "values": _series_values(r["series"], monthly)}
                for i, r in enumerate(rows)
            ]
        else:
            annual, monthly = _split_periods(set(company_details))
            annual_labels = _annual_labels(annual, budget_year)
            monthly_labels = _monthly_labels(monthly)
            annual_series = [{"name": "公司合计", "color": TREND_COLORS[0],
                              "values": _series_values(company_details, annual)}]
            monthly_series = [{"name": "公司预算", "color": TREND_COLORS[0],
                               "values": _series_values(company_details, monthly)}]
        annual_svg = _trend_svg(annual_labels, annual_series, unit)
        monthly_svg = _trend_svg(monthly_labels, monthly_series, unit)

    return render(request, "budgeting/management_trend.html", {
        "cycle": cycle,
        "report_code": report_code,
        "report_name": REPORTS[report_code],
        "view_mode": view_mode,
        "options": options,
        "row_code": row_code,
        "selected_label": selected_label,
        "unit": unit,
        "unit_label": _unit_label(unit),
        "budget_year": budget_year,
        "annual_labels": annual_labels,
        "monthly_labels": monthly_labels,
        "annual_series": annual_series,
        "monthly_series": monthly_series,
        "annual_svg": annual_svg,
        "monthly_svg": monthly_svg,
        "REPORTS": REPORTS,
    })


def _kpi_row_options(cycle, report_code):
    if not cycle:
        return []
    details = _company_value_details(cycle, report_code)
    units = {}
    for (rc, _period), detail in details.items():
        units.setdefault(rc, detail["unit"])
    labels = dict(
        NormalizedValue.objects.filter(upload__cycle=cycle, report_code=report_code)
        .values_list("row_code", "row_label")
        .distinct()
    )
    return [
        {"code": rc, "label": labels.get(rc) or rc, "unit": units.get(rc, NormalizedValue.Unit.MONEY)}
        for rc in sorted(units)
    ]


def _default_trend_row(options):
    for opt in options:
        if opt["unit"] == NormalizedValue.Unit.MONEY and "收入" in opt["label"]:
            return opt["code"]
    for opt in options:
        if opt["unit"] == NormalizedValue.Unit.MONEY:
            return opt["code"]
    return options[0]["code"] if options else ""


def _unit_label(unit):
    return {
        NormalizedValue.Unit.MONEY: "万元",
        NormalizedValue.Unit.RATIO: "%",
        NormalizedValue.Unit.COUNT: "数量",
    }.get(unit, "")


def _trend_value(detail):
    if detail["unit"] == NormalizedValue.Unit.RATIO:
        return float(Decimal(detail["value_int"]) / Decimal(RATIO_SCALE) * 100)
    if detail["unit"] == NormalizedValue.Unit.COUNT:
        return float(detail["value_int"] or 0)
    return float(cents_to_yuan(detail["value_int"]) / Decimal("10000"))


def _series_values(details, periods):
    return [_trend_value(details[p]) if p in details else None for p in periods]


def _split_periods(periods):
    annual = sorted(
        [p for p in periods if p == "YEAR" or HISTORY_PERIOD_RE.match(p)],
        key=lambda p: (1, 0) if p == "YEAR" else (0, int(p[1:]), 0 if p[0] == "A" else 1),
    )
    monthly = sorted([p for p in periods if BUDGET_MONTH_RE.match(p)])
    return annual, monthly


def _annual_label(period, budget_year):
    if period == "YEAR":
        return f"{budget_year}预算"
    nature = "实际" if period[0] == "A" else "预测"
    return f"{period[1:]}{nature}"


def _annual_labels(periods, budget_year):
    return [_annual_label(p, budget_year) for p in periods]


def _monthly_labels(periods):
    return [f"{int(p)}月" for p in periods]


def _axis_label(v, unit):
    if unit == NormalizedValue.Unit.RATIO:
        return f"{v:.0f}%"
    return f"{v:,.0f}"


def _nice_step(raw):
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _nice_ticks(lo, hi, n):
    span = hi - lo
    if span <= 0:
        return [round(lo, 4)]
    step = _nice_step(span / n)
    start = math.ceil(lo / step) * step
    out = []
    v = start
    while v <= hi + step * 1e-6:
        out.append(round(v, 6))
        v += step
    return out


def _line_path(pts):
    d = []
    pen = False
    for p in pts:
        if p is None:
            pen = False
            continue
        d.append(f"M{p[0]:.1f} {p[1]:.1f}" if not pen else f"L{p[0]:.1f} {p[1]:.1f}")
        pen = True
    return " ".join(d)


def _trend_svg(labels, series, unit):
    if not labels or not series:
        return '<svg class="trend-svg" viewBox="0 0 720 280" role="img" aria-label="暂无数据"></svg>'
    width, height = 720, 280
    ml, mr, mt, mb = 60, 16, 20, 40
    plot_w = width - ml - mr
    plot_h = height - mt - mb
    flat = [v for s in series for v in s["values"] if v is not None]
    lo = min(flat) if flat else 0
    hi = max(flat) if flat else 1
    if lo == hi:
        hi = lo + (abs(lo) or 1)
        lo = min(lo, 0)
    pad = (hi - lo) * 0.08 or 1
    lo = max(0, lo - pad) if lo >= 0 else lo - pad
    hi += pad
    ticks = _nice_ticks(lo, hi, 5)

    def y(v):
        return mt + plot_h * (1 - (v - lo) / (hi - lo))

    def x(i):
        return ml + (plot_w * (i / (len(labels) - 1)) if len(labels) > 1 else plot_w / 2)

    parts = [
        f'<svg class="trend-svg" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="趋势图">'
    ]
    for t in ticks:
        ty = y(t)
        parts.append(f'<line x1="{ml}" y1="{ty:.1f}" x2="{width - mr}" y2="{ty:.1f}" class="trend-grid"/>')
        parts.append(f'<text x="{ml - 8}" y="{ty:.1f}" class="trend-tick" text-anchor="end" '
                     f'dominant-baseline="middle">{_axis_label(t, unit)}</text>')
    for i, lb in enumerate(labels):
        parts.append(f'<text x="{x(i):.1f}" y="{height - mb + 20}" class="trend-tick" '
                     f'text-anchor="middle">{lb}</text>')
    for s in series:
        pts = [None if v is None else (x(i), y(v)) for i, v in enumerate(s["values"])]
        d = _line_path(pts)
        if d:
            parts.append(f'<path d="{d}" fill="none" stroke="{s["color"]}" stroke-width="2.5" '
                         f'stroke-linejoin="round" stroke-linecap="round"/>')
        for p in pts:
            if p:
                parts.append(f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="3.5" fill="{s["color"]}"/>')
    parts.append("</svg>")
    return "".join(parts)


@role_required("ADMIN")
def approve_upload_view(request, upload_id):
    upload = get_object_or_404(UploadVersion, id=upload_id)
    if request.method == "POST":
        try:
            approve_upload(upload, request.user)
            messages.success(request, f"已批准 {upload.project.code} 的版本并切换为正式版本。")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("management_projects")
    issues = ValidationIssue.objects.filter(run__upload=upload).order_by("severity", "code")
    return render(request, "budgeting/project_upload_detail.html", {"upload": upload, "issues": issues})


@role_required("ADMIN")
def reject_upload_view(request, upload_id):
    upload = get_object_or_404(UploadVersion, id=upload_id)
    if request.method == "POST":
        form = RejectForm(request.POST)
        if form.is_valid():
            try:
                reject_upload(upload, request.user, form.cleaned_data["reason"])
                messages.success(request, f"已打回 {upload.project.code} 的版本。")
            except ValueError as exc:
                messages.error(request, str(exc))
        return redirect("management_projects")
    return redirect("project_upload_detail", upload_id)


@role_required("ADMIN")
def management_adjustments(request):
    cycle = active_cycle()
    projects = list(Project.objects.filter(is_active=True).order_by("code"))
    report_options = list(REPORTS.items())
    preview = None
    full = None
    error = None
    form_data = {"project": "", "driver": "OCC", "to_value": "", "reason": "", "due_date": ""}
    full_data = {"project": "", "report_code": "PL_TOTAL_WINE", "reason": "", "due_date": ""}

    if request.method == "POST" and cycle:
        action = request.POST.get("action", "")

        if action in ("driver_preview", "driver_issue"):
            form_data = {
                "project": request.POST.get("project", ""),
                "driver": request.POST.get("driver", "OCC"),
                "to_value": request.POST.get("to_value", "").strip(),
                "reason": request.POST.get("reason", "").strip(),
                "due_date": request.POST.get("due_date") or "",
            }
            project = Project.objects.filter(pk=form_data["project"]).first()
            driver = form_data["driver"] if form_data["driver"] in DRIVERS else "OCC"
            try:
                if project is None:
                    raise ValueError("请选择项目")
                if form_data["to_value"] == "":
                    raise ValueError("请输入目标值")
                details = project_value_details(project, cycle, DRIVER_REPORT)
                baseline = {rc: d for (rc, period), d in details.items() if period == "YEAR"}
                snapshot = simulate_driver(baseline, driver, form_data["to_value"])
                if action == "driver_issue":
                    batch = create_driver_adjustment(
                        cycle, project, DRIVER_REPORT, driver, form_data["to_value"],
                        form_data["reason"], request.user, form_data["due_date"] or None,
                    )
                    issue_adjustment(batch, request.user)
                    messages.success(request, f"已下发 {project.name} 的「{DRIVERS[driver]}」调整。")
                    return redirect("management_adjustments")
                preview = _cascade_preview(snapshot)
            except (ValueError, KeyError) as exc:
                error = str(exc)

        elif action in ("full_preview", "full_issue"):
            full_data = {
                "project": request.POST.get("full_project", ""),
                "report_code": request.POST.get("report_code", "PL_TOTAL_WINE"),
                "reason": request.POST.get("full_reason", "").strip(),
                "due_date": request.POST.get("full_due_date") or "",
            }
            report_code = full_data["report_code"] if full_data["report_code"] in REPORTS else "PL_TOTAL_WINE"
            project = Project.objects.filter(pk=full_data["project"]).first()
            try:
                if project is None:
                    raise ValueError("请选择项目")
                base = preview_adjustment(project, cycle, report_code)
                edits = _collect_edits(base, request.POST)
                if action == "full_issue":
                    if not edits:
                        raise ValueError("未检测到任何变更")
                    if not full_data["reason"]:
                        raise ValueError("请填写调整原因")
                    batch = create_full_adjustment(
                        cycle, project, report_code, edits, full_data["reason"],
                        request.user, full_data["due_date"] or None,
                    )
                    issue_adjustment(batch, request.user)
                    messages.success(request, f"已下发 {project.name} 的「{REPORTS[report_code]}」全表调整。")
                    return redirect("management_adjustments")
                full = _full_worksheet(project, report_code, preview_adjustment(project, cycle, report_code, edits), edits)
            except (ValueError, KeyError) as exc:
                error = str(exc)
                if project is not None:
                    full = _full_worksheet(project, report_code, preview_adjustment(project, cycle, report_code), {})

    batches = (
        AdjustmentBatch.objects.filter(cycle=cycle).select_related("project").order_by("-created_at")
        if cycle else []
    )
    return render(request, "budgeting/management_adjustments.html", {
        "cycle": cycle,
        "projects": projects,
        "report_options": report_options,
        "driver_options": DRIVER_OPTIONS,
        "form_data": form_data,
        "full_data": full_data,
        "preview": preview,
        "full": full,
        "error": error,
        "batches": batches,
    })


def _collect_edits(base, post):
    """Extract changed leaf edits from the full-table form POST."""
    edits = {}
    for row in base["rows"]:
        if row["kind"] != "leaf":
            continue
        rc, unit = row["code"], row["unit"]
        for period in base["periods"]:
            raw = post.get(f"edit_{rc}_{period}", "").strip()
            if raw == "":
                continue
            engine = parse_edit(unit, raw)
            if abs(engine - row["before"].get(period, 0)) > 1e-9:
                edits.setdefault(rc, {})[period] = engine
    return edits


def _full_worksheet(project, report_code, preview, edits):
    unit_labels = {"MONEY": "万元", "COUNT": "间", "RATIO": "%"}
    rows = []
    for row in preview["rows"]:
        unit = row["unit"]
        cells = {}
        for p in preview["periods"]:
            value = row["after"].get(p, 0)
            cells[p] = edit_value(unit, value) if row["kind"] == "leaf" else display_value(unit, value)
        before_y = row["before"].get("YEAR", 0)
        after_y = row["after"].get("YEAR", 0)
        rows.append({
            "code": row["code"],
            "label": row["label"],
            "unit": unit,
            "unit_label": unit_labels.get(unit, ""),
            "kind": row["kind"],
            "cells": cells,
            "before_year": display_value(unit, before_y),
            "after_year": display_value(unit, after_y),
            "diff_year": after_y - before_y,
            "changed": abs(after_y - before_y) > 1e-9,
        })
    return {
        "project": project.code,
        "project_name": project.name,
        "report_code": report_code,
        "report_name": REPORTS.get(report_code, report_code),
        "periods": preview["periods"],
        "period_cols": [{"key": p, "label": ("年度" if p == "YEAR" else f"{int(p)}月")} for p in preview["periods"]],
        "rows": rows,
        "edited": sorted(edits),
    }


def _cascade_preview(snapshot):
    unit_labels = {"COUNT": "间", "RATIO": "%", "MONEY": "万元", "YUAN": "元"}
    rows = []
    for key, label, unit, base, proj in snapshot["rows"]:
        rows.append({
            "key": key,
            "label": label,
            "unit": unit,
            "unit_label": unit_labels.get(unit, ""),
            "baseline": format_cascade_value(unit, base),
            "projected": format_cascade_value(unit, proj),
        })
    return {
        "driver": snapshot["driver"],
        "driver_label": snapshot["driver_label"],
        "from_display": driver_value_label(snapshot["driver"], snapshot["from_value"]),
        "to_display": driver_value_label(snapshot["driver"], snapshot["to_value"]),
        "delta_room_rev_display": format_cascade_value("MONEY", snapshot["delta_room_rev"]),
        "rows": rows,
    }


@role_required("ADMIN")
def management_adjustment_issue(request, batch_id):
    batch = get_object_or_404(AdjustmentBatch, id=batch_id)
    if request.method == "POST":
        issue_adjustment(batch, request.user)
        return redirect("management_adjustments")
    lines = batch.lines.select_related("project").order_by("project__code")
    return render(request, "budgeting/management_adjustment_issue.html", {"batch": batch, "lines": lines})


@role_required("ADMIN")
def management_freeze(request):
    cycle = active_cycle()
    error = None
    if request.method == "POST" and cycle:
        try:
            freeze_cycle(cycle, request.user)
            return redirect("management_freeze")
        except ValueError as exc:
            error = str(exc)
    snapshots = FreezeSnapshot.objects.filter(cycle=cycle).order_by("-created_at") if cycle else []
    blockers = freeze_preconditions(cycle) if cycle else []
    return render(request, "budgeting/management_freeze.html", {"cycle": cycle, "snapshots": snapshots, "error": error, "blockers": blockers})


@role_required("ADMIN")
def snapshot_download(request, snapshot_id):
    snapshot = get_object_or_404(FreezeSnapshot, id=snapshot_id, status=FreezeSnapshot.Status.COMPLETE)
    zip_path = settings.BUDGET_STORAGE_ROOT / f"{snapshot.id}.zip"
    if not zip_path.exists():
        raise Http404("快照 zip 不存在")
    return FileResponse(zip_path.open("rb"), as_attachment=True, filename=f"freeze_snapshot_{snapshot.id}.zip")


@role_required("ADMIN")
def management_audit(request):
    events = AuditEvent.objects.order_by("-created_at")[:200]
    return render(request, "budgeting/management_audit.html", {"events": events})


@role_required("ADMIN")
def management_org(request):
    User = get_user_model()
    project_form = ProjectForm()
    account_form = ProjectAccountForm()
    reset_form = ResetPasswordForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create_project":
            project_form = ProjectForm(request.POST)
            if project_form.is_valid():
                project_form.save()
                messages.success(request, f"已创建项目 {project_form.instance.code}。")
                return redirect("management_org")
        elif action == "create_account":
            account_form = ProjectAccountForm(request.POST)
            if account_form.is_valid():
                data = account_form.cleaned_data
                role = "ADMIN" if data["is_admin"] else "PROJECT"
                user = User.objects.create_user(username=data["username"], password=data["password"])
                user.role = role
                user.project = None if role == "ADMIN" else data["project"]
                user.save()
                messages.success(request, f"已创建账号 {user.username}（{user.get_role_display()}）。")
                return redirect("management_org")
        elif action == "reset_password":
            reset_form = ResetPasswordForm(request.POST)
            if reset_form.is_valid():
                user = User.objects.filter(username=reset_form.cleaned_data["username"]).first()
                if not user:
                    reset_form.add_error("username", "账号不存在。")
                else:
                    user.set_password(reset_form.cleaned_data["new_password"])
                    user.save()
                    messages.success(request, f"已重置账号 {user.username} 的密码。")
                    return redirect("management_org")
    projects = Project.objects.all()
    accounts = User.objects.filter(role="PROJECT").select_related("project").order_by("username")
    return render(request, "budgeting/management_org.html", {
        "projects": projects,
        "accounts": accounts,
        "project_form": project_form,
        "account_form": account_form,
        "reset_form": reset_form,
    })


@login_required
def upload_status(request, upload_id):
    upload = get_object_or_404(UploadVersion, id=upload_id)
    if request.user.role == "PROJECT" and upload.project_id != request.user.project_id:
        return HttpResponse(status=403)
    job = ProcessingJob.objects.filter(upload=upload).order_by("-created_at").first()
    return JsonResponse({
        "upload": str(upload.id),
        "status": upload.status.lower(),
        "status_label": upload.get_status_display(),
        "upload_status": upload.status,
        "job_status": job.status if job else None,
        "job_error": job.error if job else "",
    })


def healthz(request):
    database_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        database_ok = False
    storage_ok = settings.BUDGET_STORAGE_ROOT.exists() and settings.BUDGET_STORAGE_ROOT.is_dir()
    worker = ProcessingJob.objects.order_by("-updated_at").first()
    return JsonResponse({
        "database": database_ok,
        "service": "hotel-budget",
        "storage": storage_ok,
        "libreoffice": Path(settings.SOFFICE_BIN).exists(),
        "worker_last_seen": worker.updated_at.isoformat() if worker else None,
        "now": timezone.now().isoformat(),
    })


def _display_value(value):
    if value.unit == NormalizedValue.Unit.RATIO:
        if not value.ratio_den:
            return "0.00%"
        return f"{Decimal(value.ratio_num) / Decimal(value.ratio_den):.2%}"
    if value.unit == NormalizedValue.Unit.COUNT:
        return str(int(value.value_int or 0))
    return f"{cents_to_yuan(value.value_int):,.2f}"


def _display_company_detail(detail):
    if not detail:
        return f"{cents_to_yuan(0):,.2f}"
    if detail["unit"] == NormalizedValue.Unit.RATIO:
        return f"{Decimal(detail['value_int']) / Decimal(RATIO_SCALE):.2%}"
    if detail["unit"] == NormalizedValue.Unit.COUNT:
        return str(int(detail["value_int"] or 0))
    return f"{cents_to_yuan(detail['value_int']):,.2f}"


def _display_company_total(details):
    if not details:
        return f"{cents_to_yuan(0):,.2f}"
    units = {detail["unit"] for detail in details}
    if units == {NormalizedValue.Unit.RATIO}:
        ratio_num = sum(int(detail["ratio_num"] or 0) for detail in details)
        ratio_den = sum(int(detail["ratio_den"] or 0) for detail in details)
        return f"{Decimal(_round_ratio(ratio_num, ratio_den)) / Decimal(RATIO_SCALE):.2%}"
    if units == {NormalizedValue.Unit.COUNT}:
        return str(sum(int(detail["value_int"] or 0) for detail in details))
    if units == {NormalizedValue.Unit.MONEY} and all(
        detail["ratio_num"] is not None and detail["ratio_den"] is not None
        for detail in details
    ):
        ratio_num = sum(int(detail["ratio_num"] or 0) for detail in details)
        ratio_den = sum(int(detail["ratio_den"] or 0) for detail in details)
        value_int = int(
            (Decimal(ratio_num) / Decimal(ratio_den or 1)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        ) if ratio_den else 0
        return f"{cents_to_yuan(value_int):,.2f}"
    return f"{cents_to_yuan(sum(int(detail['value_int'] or 0) for detail in details)):,.2f}"


KPI_MATCHERS = [
    ("营业收入", ("酒店总收入",)),
    ("客房收入", ("客房收入",)),
    ("餐饮收入", ("餐饮部收入合计",)),
    ("人工成本", ("人工成本",)),
    ("能耗费用", ("能源",)),
    ("出租率", ("出租率",)),
    ("经营利润", ("酒店经营利润",)),
]


def _resolve_kpi_row(row_code_to_label):
    resolved = {}
    for kpi_label, candidates in KPI_MATCHERS:
        code = None
        for cand in candidates:
            for rc, label in row_code_to_label.items():
                if label == cand or cand in label:
                    code = rc
                    break
            if code:
                break
        if code:
            resolved[kpi_label] = code
    return resolved


def _report_display_name(cycle, report_code):
    if report_code in REPORTS:
        return REPORTS[report_code]
    if cycle:
        found = NormalizedValue.objects.filter(
            upload__cycle=cycle, report_code=report_code
        ).values_list("source_sheet", flat=True).first()
        if found:
            return found
    for item in sub_table_reports(cycle):
        if item["code"] == report_code:
            return item["name"]
    return report_code


def _cell_display(unit, value_int, ratio_num, ratio_den):
    if unit == NormalizedValue.Unit.RATIO:
        return f"{Decimal(_round_ratio(ratio_num, ratio_den)) / Decimal(RATIO_SCALE):.2%}"
    if unit == NormalizedValue.Unit.COUNT:
        return str(int(value_int or 0))
    return f"{cents_to_yuan(value_int):,.2f}"


def _aggregate_row(agg, unit, value_int, ratio_num, ratio_den):
    agg["unit"] = unit
    if unit == NormalizedValue.Unit.RATIO:
        agg["ratio_num"] += int(ratio_num or 0)
        agg["ratio_den"] += int(ratio_den or 0)
    else:
        agg["value_int"] += int(value_int or 0)
    return agg


def _dashboard_data(cycle, report_code):
    if not cycle:
        return [], []
    uploads = ProjectCycle.objects.filter(
        cycle=cycle,
        current_upload__cycle=cycle,
        current_upload__project=F("project"),
        current_upload__status=UploadVersion.Status.APPROVED,
        project__is_active=True,
    ).select_related("project", "current_upload").order_by("project__code")
    if not uploads:
        return [], []
    upload_ids = [u.current_upload_id for u in uploads]
    per = {}
    labels = {}
    for v in NormalizedValue.objects.filter(upload_id__in=upload_ids, report_code=report_code, period="YEAR"):
        pr = per.setdefault(v.upload.project_id, {})
        a = pr.setdefault(v.row_code, {"unit": None, "value_int": 0, "ratio_num": 0, "ratio_den": 0})
        _aggregate_row(a, v.unit, v.value_int, v.ratio_num, v.ratio_den)
        labels.setdefault(v.row_code, v.row_label or v.row_code)

    comp = {}
    for _pid, pr in per.items():
        for rc, a in pr.items():
            c = comp.setdefault(rc, {"unit": None, "value_int": 0, "ratio_num": 0, "ratio_den": 0})
            _aggregate_row(c, a["unit"], a["value_int"], a["ratio_num"], a["ratio_den"])
    kpi_rows = _resolve_kpi_row(labels)
    kpis = []
    for kpi_label, rc in kpi_rows.items():
        a = comp[rc]
        kpis.append({
            "row_code": rc, "label": kpi_label, "kind": a["unit"],
            "value_int": a["value_int"], "ratio_num": a["ratio_num"], "ratio_den": a["ratio_den"],
            "display": _cell_display(a["unit"], a["value_int"], a["ratio_num"], a["ratio_den"]),
        })
    kpi_codes = [k["row_code"] for k in kpis]
    projects = []
    for u in uploads:
        pr = per.get(u.project_id, {})
        cells = [
            _cell_display(pr[rc]["unit"], pr[rc]["value_int"], pr[rc]["ratio_num"], pr[rc]["ratio_den"])
            if rc in pr else "—"
            for rc in kpi_codes
        ]
        projects.append({"code": u.project.code, "name": u.project.name, "cells": cells})
    return kpis, projects
