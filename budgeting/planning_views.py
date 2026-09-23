from budgeting.services.project_scope import cycle_projects
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from budgeting.services.template_library import get_distribution_template, distribution_template_path, publish_template
from budgeting.services.template_paths import resolve_template_path
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from budgeting.cockpit_views import admin_required, _cycle, _report
from budgeting.models import BudgetCycle, NormalizedValue, Project, REPORTS
from budgeting.services.metrics import METRICS
from budgeting.services.template_delivery import signed_template_copy
from budgeting.services.trends import (
    _display_value, aggregate_metric, latest_report_uploads, build_trend,
    metric_choices,
)

OVERVIEW_METRICS = (
    "revenue_total", "revenue_room", "revenue_fb", "revenue_other",
    "revenue_rent", "profit_gop", "profit_operating", "profit_npi",
    "occ", "adr", "revpar",
)


def _project_id(request, cycle):
    raw = request.GET.get("project_id", "")
    if not raw:
        return None
    try:
        return get_object_or_404(cycle_projects(cycle), pk=int(raw)).pk
    except ValueError as exc:
        raise Http404("项目不存在") from exc


def _context(request):
    cycle = _cycle(request)
    project_id = _project_id(request, cycle)
    report = _report(request)
    uploads = latest_report_uploads(cycle, project_id=project_id) if cycle else []
    upload_ids = [item.current_upload_id for item in uploads]
    demo = NormalizedValue.objects.filter(upload_id__in=upload_ids, source_cell="DEMO").exists()
    return {
        "cycle": cycle,
        "cycles": BudgetCycle.objects.order_by("-budget_year", "-revision_no"),
        "projects": cycle_projects(cycle),
        "selected_project_id": project_id,
        "report_code": report,
        "report_choices": [{"code": code, "label": label} for code, label in REPORTS.items()],
        "display_unit": "wan",
        "demo_mode": demo,
    }


def _comparison(cycle, code, report, project_id=None):
    current = aggregate_metric(cycle, code, report, project_id=project_id, data_scope="latest")
    prior = aggregate_metric(cycle, code, report, year=cycle.budget_year - 1,
                             kind="FORECAST", project_id=project_id, data_scope="latest")
    value, base = current["value"], prior["value"]
    if value is None or base is None:
        return "—", "缺少对照数据"
    if set(current["project_ids"]) != set(prior["project_ids"]):
        return "—", "对照项目不一致"
    spec = METRICS[code]
    delta = value - base
    if spec["unit"] == "RATIO":
        return f"{Decimal(delta) / 100:+.2f} 个百分点", "—"
    delta_label = _display_value(abs(delta), spec["unit"], "wan", code, spec.get("aggregation"))
    delta_label = ("+" if delta > 0 else "−" if delta < 0 else "") + delta_label
    growth = "基期为零" if base == 0 else f"{Decimal(delta) / abs(base) * 100:+.2f}%"
    return delta_label, growth


@admin_required
def planning_overview(request):
    context = _context(request)
    cycle, report = context["cycle"], context["report_code"]
    project_id = context["selected_project_id"]
    selected = request.GET.get("metric", "revenue_total")
    if selected not in METRICS:
        selected = "revenue_total"
    rows = []
    for code in OVERVIEW_METRICS:
        payload = build_trend(cycle, code, report, project_id, "wan", data_scope="latest")
        delta, change = _comparison(cycle, code, report, project_id) if cycle else ("—", "—")
        rows.append({
            "metric": code, "label": payload["metric_label"], "unit": payload["unit_label"],
            "cells": payload["annual"], "delta_display": delta, "change_display": change,
        })
    trend = build_trend(cycle, selected, report, project_id, "wan", data_scope="latest")
    context.update({
        "planning_rows": rows, "comparison_years": trend["years"],
        "trend_payload": trend, "selected_metric": selected,
        "metric_choices": metric_choices(report), "coverage": trend["coverage"],
    })
    return render(request, "budgeting/planning_overview.html", context)


@admin_required
def planning_projects(request):
    context = _context(request)
    cycle, report = context["cycle"], context["report_code"]
    uploads = {item.project_id: item.current_upload for item in latest_report_uploads(cycle)} if cycle else {}
    rows = []
    query = request.GET.get("q", "").strip()
    projects = context["projects"]
    if query:
        from django.db.models import Q
        projects = projects.filter(Q(name__icontains=query) | Q(code__icontains=query))
    for project in projects:
        row = {"project": project, "upload": uploads.get(project.pk)}
        for key, code in (("revenue", "revenue_total"), ("gop", "profit_gop"),
                          ("npi", "profit_npi"), ("occ", "occ"), ("adr", "adr")):
            trend = build_trend(cycle, code, report, project.pk, "wan", data_scope="latest")
            row[f"{key}_display"] = trend["annual"][-1]["display"] if trend["annual"] else "—"
        row["growth_display"] = _comparison(cycle, "revenue_total", report, project.pk)[1] if cycle else "—"
        params = {"project_id": project.pk, "report_code": report}
        if cycle:
            params["cycle"] = cycle.pk
        row["detail_url"] = reverse("report_catalog") + "?" + urlencode(params)
        row["analysis_url"] = reverse("planning_overview") + "?" + urlencode(params)
        rows.append(row)
    context.update({"project_rows": rows, "query": query})
    return render(request, "budgeting/planning_projects.html", context)


@admin_required
@require_http_methods(["GET", "POST"])
def planning_templates(request):
    context = _context(request)
    cycle = context["cycle"]
    if request.method == "POST":
        if not cycle:
            messages.error(request, "请先选择预算版本。")
        elif not request.FILES.get("template_file"):
            messages.error(request, "请选择需要发布的 Excel 模板。")
        else:
            try:
                publish_template(cycle, request.FILES["template_file"], request.user, request.POST.get("template_name", ""))
                messages.success(request, "填报模板已发布，项目现在可以下载。此前发布的文件仍保留。")
                return redirect(reverse("planning_templates") + "?" + urlencode({"cycle": cycle.pk}))
            except ValidationError as exc:
                messages.error(request, "；".join(exc.messages))
    template = cycle.template if cycle and cycle.template_id else None
    distributed = get_distribution_template(cycle)
    has_template = bool(distribution_template_path(distributed).is_file()) if distributed else bool(template and resolve_template_path(template.file_path).is_file())
    downloads = []
    if has_template:
        for project in context["projects"]:
            url = reverse("planning_template_download", args=[project.pk]) + "?" + urlencode({"cycle": cycle.pk})
            downloads.append({"project": project, "url": url})
    context.update({
        "template": template, "has_template": has_template, "downloads": downloads,
        "distributed": distributed,
        "template_history": cycle.distributed_templates.order_by("-created_at", "-pk")[:10] if cycle else [],
    })
    return render(request, "budgeting/planning_templates.html", context)


@admin_required
def planning_template_download(request, project_id):
    from budgeting.services.template_library import download_distribution_template
    cycle = _cycle(request)
    if not cycle or not cycle.template_id:
        messages.info(request, "此预算版本尚未发布可用填报模板。")
        return redirect("planning_templates")
    project = get_object_or_404(cycle_projects(cycle), pk=project_id)
    try:
        distributed = get_distribution_template(cycle)
        if distributed:
            path = download_distribution_template(distributed, project, cycle)
        else:
            path = signed_template_copy(cycle.template, project, cycle)
        return FileResponse(path.open("rb"), as_attachment=True,
                            filename=f"{project.name}-{cycle.budget_year}年预算-R{cycle.revision_no}{path.suffix}")
    except (FileNotFoundError, ValueError, ValidationError):
        messages.error(request, "模板暂时无法下载，请检查已发布文件和历史数据。")
        return redirect(reverse("planning_templates") + "?" + urlencode({"cycle": cycle.pk}))


@admin_required
def planning_entry(request):
    return redirect("planning_overview")
