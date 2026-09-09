from __future__ import annotations

from urllib.parse import urlencode

from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from budgeting.cockpit_views import _cycle, admin_required
from budgeting.models import BudgetCycle, Project, UploadVersion
from budgeting.services.workflow import audit
from budgeting.services.workpaper_exports import (
    WorkpaperExportError,
    build_cycle_workpaper_zip,
    check_workpaper_file,
    cleanup_workpaper_archive,
    download_filename,
    normalize_selection,
    preflight_cycle_workpapers,
    resolve_workpaper_file,
    select_upload,
    selection_label,
)


def _request_cycle(request):
    cycle = _cycle(request)
    if cycle is None:
        raise Http404("当前没有可用的预算周期。")
    return cycle


def _request_selection(request):
    try:
        return normalize_selection(
            request.GET.get("selection") or request.GET.get("version")
        )
    except WorkpaperExportError as exc:
        raise Http404(str(exc)) from exc


def _download_url(project_id, artifact, cycle, selection, upload_id=None):
    params = {"cycle": cycle.pk, "selection": selection}
    if upload_id:
        params["upload_id"] = str(upload_id)
    query = urlencode(params)
    return f"{reverse('management_workpaper_download', args=[project_id, artifact])}?{query}"


def _try_upload(project, cycle, selection):
    try:
        return select_upload(project, cycle, selection)
    except WorkpaperExportError:
        return None


def _artifact_entry(project, cycle, upload, artifact, selection):
    if upload is None:
        return None
    availability = check_workpaper_file(upload, artifact)
    return {
        "available": availability.available,
        "message": availability.message,
        "code": availability.code,
        "url": (
            _download_url(
                project.pk,
                artifact,
                cycle,
                selection,
                upload_id=upload.id if selection == "historical" else None,
            )
            if availability.available
            else None
        ),
    }


def _version_entry(project, cycle, upload, selection):
    if upload is None:
        return None
    return {
        "upload": upload,
        "selection": selection,
        "label": selection_label(selection),
        "original": _artifact_entry(project, cycle, upload, "original", selection),
        "recalculated": _artifact_entry(
            project, cycle, upload, "recalculated", selection
        ),
    }


def _row(project, cycle):
    approved = _try_upload(project, cycle, "approved")
    latest = _try_upload(project, cycle, "latest")
    history = [
        _version_entry(project, cycle, upload, "historical")
        for upload in UploadVersion.objects.filter(project=project, cycle=cycle).order_by(
            "-created_at", "-id"
        )
    ]
    approved_entry = _version_entry(project, cycle, approved, "approved")
    latest_entry = _version_entry(project, cycle, latest, "latest")
    return {
        "project": project,
        "approved_upload": approved,
        "latest_upload": latest,
        "approved_entry": approved_entry,
        "latest_entry": latest_entry,
        "history": history,
        "approved_label": selection_label("approved"),
        "latest_label": selection_label("latest"),
        "approved_original_url": _download_url(
            project.pk, "original", cycle, "approved"
        )
        if approved_entry and approved_entry["original"]["available"]
        else None,
        "approved_recalculated_url": _download_url(
            project.pk, "recalculated", cycle, "approved"
        )
        if approved_entry and approved_entry["recalculated"]["available"]
        else None,
        "latest_original_url": _download_url(project.pk, "original", cycle, "latest")
        if latest_entry and latest_entry["original"]["available"]
        else None,
        "latest_recalculated_url": _download_url(
            project.pk, "recalculated", cycle, "latest"
        )
        if latest_entry and latest_entry["recalculated"]["available"]
        else None,
    }


@admin_required
def workpaper_exports(request):
    cycle = _cycle(request)
    cycles = BudgetCycle.objects.order_by("-budget_year", "-revision_no")
    all_projects = Project.objects.filter(is_active=True).order_by("code")
    raw_project_id = request.GET.get("project_id")
    selected_project_id = ""
    projects = all_projects
    if raw_project_id:
        try:
            selected_project_id = str(int(raw_project_id))
            projects = all_projects.filter(pk=int(raw_project_id))
        except (TypeError, ValueError):
            raise Http404("项目不存在。")
    rows = [_row(project, cycle) for project in projects] if cycle else []
    project_ids = list(projects.values_list("pk", flat=True)) if cycle else None
    batch_preflights = {}
    if cycle:
        for selection in ("approved", "latest"):
            batch_preflights[selection] = preflight_cycle_workpapers(
                cycle, selection, project_ids=project_ids
            )
    batch_query = {"cycle": cycle.pk} if cycle else {}
    if selected_project_id:
        batch_query["project_id"] = selected_project_id
    return render(
        request,
        "budgeting/workpaper_exports.html",
        {
            "cycle": cycle,
            "cycles": cycles,
            "all_projects": all_projects,
            "selected_project_id": selected_project_id,
            "rows": rows,
            "selection_label": selection_label("approved"),
            "latest_selection_label": selection_label("latest"),
            "batch_approved_preflight": batch_preflights.get("approved"),
            "batch_latest_preflight": batch_preflights.get("latest"),
            "batch_approved_url": (
                f"{reverse('management_workpaper_batch_download')}?"
                f"{urlencode({**batch_query, 'selection': 'approved'})}"
            )
            if cycle and batch_preflights["approved"]["ready"]
            else None,
            "batch_latest_url": (
                f"{reverse('management_workpaper_batch_download')}?"
                f"{urlencode({**batch_query, 'selection': 'latest'})}"
            )
            if cycle and batch_preflights["latest"]["ready"]
            else None,
        },
    )


@admin_required
def workpaper_download(request, project_id, artifact):
    cycle = _request_cycle(request)
    selection = _request_selection(request)
    upload_id = request.GET.get("upload_id")
    if upload_id:
        selection = "historical"
    project = get_object_or_404(Project, pk=project_id, is_active=True)
    try:
        upload = select_upload(
            project,
            cycle,
            selection,
            upload_id=upload_id,
        )
        workpaper = resolve_workpaper_file(upload, artifact)
    except WorkpaperExportError as exc:
        return HttpResponse(
            str(exc), status=404, content_type="text/plain; charset=utf-8"
        )
    audit(
        request.user,
        "WORKPAPER_ORIGINAL_DOWNLOADED"
        if workpaper.artifact == "original"
        else "WORKPAPER_RECALCULATED_DOWNLOADED",
        "UploadVersion",
        upload.id,
        {
            "selection": selection,
            "selection_label": selection_label(selection),
            "artifact": workpaper.artifact,
            "sha256": workpaper.sha256,
            "size": workpaper.size,
        },
        project=project,
        cycle=cycle,
        upload=upload,
    )
    response = FileResponse(
        workpaper.path.open("rb"),
        as_attachment=True,
        filename=download_filename(workpaper),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["X-Workpaper-Selection"] = selection
    return response


def _project_ids(request):
    values = request.GET.getlist("project_id")
    if len(values) == 1 and "," in values[0]:
        values = [item.strip() for item in values[0].split(",") if item.strip()]
    if not values:
        return None
    try:
        return [int(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise Http404("项目筛选参数无效。") from exc


@admin_required
def workpaper_batch_download(request):
    cycle = _request_cycle(request)
    selection = _request_selection(request)
    try:
        archive = build_cycle_workpaper_zip(
            cycle,
            selection,
            project_ids=_project_ids(request),
        )
    except WorkpaperExportError as exc:
        return HttpResponse(
            str(exc), status=404, content_type="text/plain; charset=utf-8"
        )
    audit(
        request.user,
        "WORKPAPER_BATCH_DOWNLOADED",
        "BudgetCycle",
        cycle.pk,
        {
            "selection": selection,
            "selection_label": selection_label(selection),
            "file_count": len(archive.manifest["files"]),
            "omitted_project_count": len(archive.manifest["omitted_projects"]),
            "manifest": archive.manifest,
        },
        cycle=cycle,
    )
    response = FileResponse(
        archive.path.open("rb"),
        as_attachment=True,
        filename=f"{cycle.budget_year}-预算底稿-{selection}.zip",
        content_type="application/zip",
    )
    response["X-Workpaper-Selection"] = selection
    response._resource_closers.append(
        lambda path=archive.path: cleanup_workpaper_archive(path)
    )
    return response
