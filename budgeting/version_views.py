from __future__ import annotations

from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from budgeting.models import BudgetCycle, Project, UploadVersion
from budgeting.services.budget_versions import (
    budget_version_accepts_uploads,
    budget_version_rows,
    close_budget_version,
    create_and_open_budget_version,
    selected_project_upload,
    version_label,
)


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_admin_role:
            raise Http404()
        return view(request, *args, **kwargs)

    return wrapped


def _cycle(cycle_id):
    return get_object_or_404(BudgetCycle, pk=cycle_id)


@admin_required
def budget_versions(request):
    cycles = list(BudgetCycle.objects.select_related("template").order_by("-budget_year", "-revision_no"))
    selected = None
    raw_cycle = request.GET.get("cycle")
    if raw_cycle:
        selected = get_object_or_404(BudgetCycle, pk=raw_cycle)
    elif cycles:
        selected = cycles[0]

    rows = budget_version_rows(selected) if selected else []
    uploaded_count = sum(row.has_uploaded for row in rows)
    reportable_count = sum(row.can_view_report for row in rows)
    return render(
        request,
        "budgeting/budget_versions.html",
        {
            "cycle": selected,
            "cycles": cycles,
            "rows": rows,
            "uploaded_count": uploaded_count,
            "reportable_count": reportable_count,
            "project_count": len(rows),
            "version_label": version_label(selected) if selected else "",
            "version_is_open": budget_version_accepts_uploads(selected) if selected else False,
        },
    )


@admin_required
def budget_version_open(request):
    if request.method != "POST":
        return redirect("management_budget_versions")
    try:
        budget_year = int(request.POST.get("budget_year", ""))
    except (TypeError, ValueError):
        messages.error(request, "请输入有效的预算年度。")
        return redirect("management_budget_versions")
    try:
        cycle = create_and_open_budget_version(
            budget_year=budget_year,
            name=request.POST.get("name", ""),
            actor=request.user,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("management_budget_versions")
    messages.success(request, f"已开放 {version_label(cycle)}，各项目可上传本版预算。")
    return redirect(f"{reverse('management_budget_versions')}?{urlencode({'cycle': cycle.pk})}")


@admin_required
def budget_version_close(request, cycle_id):
    if request.method != "POST":
        return redirect("management_budget_versions")
    cycle = _cycle(cycle_id)
    try:
        close_budget_version(cycle, actor=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"已停止 {version_label(cycle)} 的项目上传。")
    return redirect(f"{reverse('management_budget_versions')}?{urlencode({'cycle': cycle.pk})}")


@admin_required
def budget_version_project(request, cycle_id, project_id):
    cycle = _cycle(cycle_id)
    project = get_object_or_404(Project, pk=project_id, is_active=True)
    raw_upload_id = request.GET.get("upload_id")
    if raw_upload_id:
        upload = get_object_or_404(
            UploadVersion,
            pk=raw_upload_id,
            cycle=cycle,
            project=project,
        )
    else:
        upload = selected_project_upload(project, cycle)

    report_links = []
    if upload and upload.status in (
        UploadVersion.Status.VALIDATED,
        UploadVersion.Status.SUBMITTED,
        UploadVersion.Status.APPROVED,
    ):
        for code, label in (
            ("PL_TOTAL_WINE", "酒店损益总表（含名酒）"),
            ("PL_TOTAL_NOWINE", "酒店损益总表（不含名酒）"),
            ("PL_ZZ_WINE", "损益表（含名酒）（拆中智）"),
            ("PL_ZZ_NOWINE", "损益表（不含名酒）（拆中智）"),
        ):
            query = urlencode(
                {"cycle": cycle.pk, "project_id": project.pk, "upload_id": upload.pk}
            )
            report_links.append(
                {
                    "code": code,
                    "label": label,
                    "url": f"{reverse('management_version_report')}?{query}&report_code={code}",
                }
            )

    return render(
        request,
        "budgeting/budget_version_project.html",
        {
            "cycle": cycle,
            "project": project,
            "upload": upload,
            "report_links": report_links,
            "version_label": version_label(cycle),
        },
    )
