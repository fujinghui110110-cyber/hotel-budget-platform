from django.db.models import Q
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from budgeting.models import BudgetCycle, Project, UploadVersion, REPORTS, HistoricalImport
from budgeting.services.data_read_audit import build_upload_audit


@login_required
def data_audit(request):
    is_admin = request.user.is_superuser or request.user.role == "ADMIN"
    if not is_admin and (request.user.role != "PROJECT" or not request.user.project_id):
        raise Http404
    cycles = BudgetCycle.objects.order_by("-budget_year", "-revision_no")
    cycle_id = request.GET.get("cycle")
    if cycle_id and not cycle_id.isdigit():
        raise Http404
    cycle = get_object_or_404(cycles, pk=cycle_id) if cycle_id else cycles.first()
    projects = Project.objects.filter(is_active=True).order_by("code")
    if not is_admin:
        projects = projects.filter(pk=request.user.project_id)
    project_id = request.GET.get("project_id")
    if project_id and not project_id.isdigit():
        raise Http404
    selected_project = get_object_or_404(projects, pk=project_id) if project_id else None
    if not is_admin:
        selected_project = projects.first()
    project_rows = []
    selected_upload = None
    selected_audit = None
    for project in projects:
        upload = UploadVersion.objects.filter(cycle=cycle, project=project).order_by("-created_at", "-id").first() if cycle else None
        pending = upload and upload.status in (UploadVersion.Status.RECEIVED, UploadVersion.Status.PROCESSING)
        audit = build_upload_audit(upload) if upload and not pending else None
        project_rows.append({"project": project, "upload": upload, "pending": pending,
                             "summary": audit["summary"] if audit else None,
                             "url": "?" + urlencode({"cycle": cycle.pk, "project_id": project.pk}) if cycle else ""})
        if selected_project and project.pk == selected_project.pk:
            selected_upload, selected_audit = upload, audit
    reason = request.GET.get("reason", "")
    report_code = request.GET.get("report_code", "")
    query = request.GET.get("q", "").strip()
    issues = selected_audit["issues"] if selected_audit else []
    reasons = sorted({(item["reason_code"], item["reason"]) for item in issues})
    reports = sorted({(item["report_code"], REPORTS.get(item["report_code"], item["report_code"])) for item in issues if item.get("report_code")})
    issues = [dict(item, report_label=REPORTS.get(item["report_code"], item["report_code"]), period_display=(f"{cycle.budget_year}预算全年" if item["period"] in ("YEAR", "FY") else f"{cycle.budget_year}预算{int(item["period"])}月" if str(item["period"]).isdigit() and 1 <= int(item["period"]) <= 12 else item["period"])) for item in issues]
    if reason:
        issues = [item for item in issues if item["reason_code"] == reason]
    if report_code:
        issues = [item for item in issues if item["report_code"] == report_code]
    if query:
        issues = [item for item in issues if query.casefold() in " ".join(str(item.get(key, "")) for key in ("label", "row_code", "source_sheet", "source_cell", "message")).casefold()]
    page = Paginator(issues, 80).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    history_batches = HistoricalImport.objects.filter(project__in=projects).filter(
        Q(active=True) | Q(confirmed_at__isnull=True)).select_related("project")
    if selected_project:
        history_batches = history_batches.filter(project=selected_project)
    if cycle:
        history_batches = history_batches.filter(data_year__lt=cycle.budget_year, data_year__gte=cycle.budget_year-3)
    history_rows = []
    for batch in history_batches[:100]:
        mapping = batch.proposal.get("confirmed_mapping", {})
        skipped = [row["source_label"] for row in batch.proposal.get("rows", []) if batch.confirmed_at and not mapping.get(row["source_key"])]
        history_rows.append({"batch": batch, "issues": batch.proposal.get("issues", []), "skipped": skipped})
    return render(request, "budgeting/data_audit.html", {
        "history_rows": history_rows,
        "cycle": cycle, "cycles": cycles, "project_rows": project_rows,
        "selected_project": selected_project, "upload": selected_upload,
        "audit": selected_audit, "issue_page": page, "reasons": reasons, "reports": reports,
        "selected_reason": reason, "selected_report": report_code, "query": query,
        "page_query": params.urlencode(), "is_admin": is_admin,
    })
