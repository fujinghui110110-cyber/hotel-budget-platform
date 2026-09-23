from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from django.conf import settings
from django.utils import timezone

from budgeting.models import BudgetCycle, Project, ProjectCycle, UploadVersion


SELECTION_APPROVED = "approved"
SELECTION_LATEST = "latest"
SELECTION_HISTORICAL = "historical"
_SELECTION_ALIASES = {
    "approved": SELECTION_APPROVED,
    "current": SELECTION_APPROVED,
    "current_approved": SELECTION_APPROVED,
    "current-approved": SELECTION_APPROVED,
    "latest": SELECTION_LATEST,
    "latest_upload": SELECTION_LATEST,
    "latest-upload": SELECTION_LATEST,
    "historical": SELECTION_HISTORICAL,
    "history": SELECTION_HISTORICAL,
    "specific": SELECTION_HISTORICAL,
}

_ARTIFACT_ALIASES = {
    "original": "original",
    "raw": "original",
    "source": "original",
    "original_xlsx": "original",
    "original.xlsx": "original",
    "recalculated": "recalculated",
    "recalc": "recalculated",
    "recalculated_xlsx": "recalculated",
    "recalculated.xlsx": "recalculated",
}


class WorkpaperExportError(ValueError):
    def __init__(self, message: str, *, code: str = "EXPORT_ERROR") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WorkpaperFile:
    upload: UploadVersion
    artifact: str
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class WorkpaperArchive:
    path: Path
    manifest: dict


@dataclass(frozen=True)
class WorkpaperAvailability:
    upload: UploadVersion
    artifact: str
    available: bool
    message: str = ""
    code: str = ""
    path: Path | None = None


def normalize_selection(value: str | None) -> str:
    raw = (value or SELECTION_APPROVED).strip().lower()
    try:
        return _SELECTION_ALIASES[raw]
    except KeyError as exc:
        raise WorkpaperExportError(
            "版本选择无效，请选择‘当前批准版本’或明确选择‘最新上传’。",
            code="INVALID_SELECTION",
        ) from exc


def normalize_artifact(value: str | None) -> str:
    raw = (value or "").strip().lower()
    try:
        return _ARTIFACT_ALIASES[raw]
    except KeyError as exc:
        raise WorkpaperExportError(
            "底稿类型无效，仅支持原始 XLSX 或重算 XLSX。",
            code="INVALID_ARTIFACT",
        ) from exc


def selection_label(selection: str) -> str:
    return {
        SELECTION_APPROVED: "当前批准版本",
        SELECTION_LATEST: "最新上传版本",
        SELECTION_HISTORICAL: "指定历史版本",
    }[normalize_selection(selection)]


def _project(project: Project | int) -> Project:
    if isinstance(project, Project):
        return project
    try:
        return Project.objects.get(pk=int(project), is_active=True)
    except (Project.DoesNotExist, TypeError, ValueError) as exc:
        raise WorkpaperExportError(
            "项目不存在或已停用。", code="PROJECT_NOT_FOUND"
        ) from exc


def _cycle(cycle: BudgetCycle | int) -> BudgetCycle:
    if isinstance(cycle, BudgetCycle):
        return cycle
    try:
        return BudgetCycle.objects.get(pk=int(cycle))
    except (BudgetCycle.DoesNotExist, TypeError, ValueError) as exc:
        raise WorkpaperExportError("预算周期不存在。", code="CYCLE_NOT_FOUND") from exc


def _approved_upload(project: Project, cycle: BudgetCycle) -> UploadVersion | None:
    project_cycle = (
        ProjectCycle.objects.select_related("current_upload")
        .filter(project=project, cycle=cycle)
        .first()
    )
    current = project_cycle.current_upload if project_cycle else None
    if (
        current is not None
        and current.project_id == project.pk
        and current.cycle_id == cycle.pk
        and current.status == UploadVersion.Status.APPROVED
    ):
        return current

    approved = list(
        UploadVersion.objects.filter(
            project=project,
            cycle=cycle,
            status=UploadVersion.Status.APPROVED,
        ).order_by("-approved_at", "-created_at", "-id")[:2]
    )
    if len(approved) == 1:
        return approved[0]
    if len(approved) > 1:
        raise WorkpaperExportError(
            f"项目 {project.code} 存在多个已批准版本但没有有效的当前指针，请先明确指定版本。",
            code="AMBIGUOUS_APPROVED_VERSION",
        )
    return None


def _latest_upload(project: Project, cycle: BudgetCycle) -> UploadVersion | None:
    return (
        UploadVersion.objects.filter(project=project, cycle=cycle)
        .order_by("-created_at", "-id")
        .first()
    )


def select_upload(
    project: Project | int,
    cycle: BudgetCycle | int,
    selection: str | None = SELECTION_APPROVED,
    *,
    upload_id: str | None = None,
) -> UploadVersion:
    project_obj = _project(project)
    cycle_obj = _cycle(cycle)
    normalized = normalize_selection(selection)

    if upload_id:
        upload = UploadVersion.objects.filter(
            id=upload_id,
            project=project_obj,
            cycle=cycle_obj,
        ).first()
        if upload is None:
            raise WorkpaperExportError(
                "指定的预算版本不存在，或不属于当前项目和预算周期。",
                code="UPLOAD_NOT_FOUND",
            )
        return upload

    if normalized == SELECTION_HISTORICAL:
        raise WorkpaperExportError(
            "历史版本下载必须指定 upload_id。",
            code="HISTORICAL_UPLOAD_REQUIRED",
        )

    upload = (
        _approved_upload(project_obj, cycle_obj)
        if normalized == SELECTION_APPROVED
        else _latest_upload(project_obj, cycle_obj)
    )
    if upload is None:
        if normalized == SELECTION_APPROVED:
            raise WorkpaperExportError(
                f"项目 {project_obj.code} 在当前周期没有当前批准版本；如需下载最新上传，请明确选择 latest。",
                code="NO_CURRENT_APPROVED_VERSION",
            )
        raise WorkpaperExportError(
            f"项目 {project_obj.code} 在当前周期没有上传版本。",
            code="NO_LATEST_UPLOAD",
        )
    return upload


def _storage_root() -> Path:
    return Path(settings.BUDGET_STORAGE_ROOT).expanduser().resolve()


def _safe_storage_path(
    relative_path: str, *, upload: UploadVersion, artifact: str
) -> Path:
    if not relative_path or not isinstance(relative_path, str):
        raise WorkpaperExportError(
            f"{upload.project.code} 的{artifact}文件路径为空，无法下载。",
            code="MISSING_FILE_PATH",
        )
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise WorkpaperExportError(
            f"{upload.project.code} 的{artifact}文件路径不安全，已拒绝下载。",
            code="UNSAFE_FILE_PATH",
        )
    root = _storage_root()
    path = (root / candidate).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WorkpaperExportError(
            f"{upload.project.code} 的{artifact}文件路径超出存储目录，已拒绝下载。",
            code="UNSAFE_FILE_PATH",
        ) from exc
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise WorkpaperExportError(
            f"{upload.project.code} 的{artifact}文件不是 XLSX 文件，无法下载。",
            code="INVALID_FILE_TYPE",
        )
    if not path.is_file():
        raise WorkpaperExportError(
            f"{upload.project.code} 的{artifact}文件不存在：{relative_path}。",
            code="MISSING_FILE",
        )
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_workpaper_file(upload: UploadVersion, artifact: str) -> WorkpaperFile:
    normalized = normalize_artifact(artifact)
    stored = (
        upload.original_path if normalized == "original" else upload.recalculated_path
    )
    path = _safe_storage_path(stored, upload=upload, artifact=normalized)
    return WorkpaperFile(
        upload=upload,
        artifact=normalized,
        path=path,
        sha256=sha256_file(path),
        size=path.stat().st_size,
    )


def check_workpaper_file(upload: UploadVersion, artifact: str) -> WorkpaperAvailability:
    normalized = normalize_artifact(artifact)
    stored = (
        upload.original_path if normalized == "original" else upload.recalculated_path
    )
    try:
        path = _safe_storage_path(stored, upload=upload, artifact=normalized)
    except WorkpaperExportError as exc:
        return WorkpaperAvailability(
            upload=upload,
            artifact=normalized,
            available=False,
            message=str(exc),
            code=exc.code,
        )
    return WorkpaperAvailability(
        upload=upload,
        artifact=normalized,
        available=True,
        path=path,
    )


def _safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", value or "").strip("._")
    return cleaned or fallback


def download_filename(workpaper: WorkpaperFile) -> str:
    project_code = _safe_name(workpaper.upload.project.code, "project")
    cycle_year = workpaper.upload.cycle.budget_year
    suffix = "原始" if workpaper.artifact == "original" else "重算"
    return f"{project_code}-{cycle_year}-{str(workpaper.upload.id)[:8]}-{suffix}{workpaper.path.suffix.lower()}"


def _relative_source_path(path: Path) -> str:
    try:
        return str(path.relative_to(_storage_root()))
    except ValueError:
        return str(path.name)


def _manifest_file(workpaper: WorkpaperFile, archive_path: str) -> dict:
    upload = workpaper.upload
    declared = upload.sha256 or ""
    return {
        "project_id": upload.project_id,
        "project_code": upload.project.code,
        "project_name": upload.project.name,
        "cycle_id": upload.cycle_id,
        "budget_year": upload.cycle.budget_year,
        "upload_id": str(upload.id),
        "version_name": upload.original_name or Path(upload.original_path).name,
        "status": upload.status,
        "status_label": upload.get_status_display(),
        "artifact": workpaper.artifact,
        "archive_path": archive_path,
        "source_path": _relative_source_path(workpaper.path),
        "size": workpaper.size,
        "sha256": workpaper.sha256,
        "declared_original_sha256": declared,
        "declared_hash_matches": bool(declared) and declared == workpaper.sha256
        if workpaper.artifact == "original"
        else None,
    }


def _project_queryset(
    cycle: BudgetCycle,
    project_ids: Sequence[int] | None = None,
) -> list[Project]:
    queryset = Project.objects.filter(is_active=True).order_by("code")
    if project_ids is not None:
        normalized_ids = []
        for value in project_ids:
            try:
                normalized_ids.append(int(value))
            except (TypeError, ValueError) as exc:
                raise WorkpaperExportError(
                    "项目筛选参数无效。", code="INVALID_PROJECT_FILTER"
                ) from exc
        queryset = queryset.filter(pk__in=normalized_ids)
    return list(queryset)


def _selected_for_batch(
    cycle: BudgetCycle,
    selection: str,
    project_ids: Sequence[int] | None = None,
) -> tuple[list[UploadVersion], list[dict]]:
    selected: list[UploadVersion] = []
    omitted: list[dict] = []
    for project in _project_queryset(cycle, project_ids):
        try:
            selected.append(select_upload(project, cycle, selection))
        except WorkpaperExportError as exc:
            if exc.code == "AMBIGUOUS_APPROVED_VERSION":
                raise
            omitted.append(
                {
                    "project_id": project.pk,
                    "project_code": project.code,
                    "project_name": project.name,
                    "status": "NO_SELECTED_VERSION",
                    "reason": str(exc),
                }
            )
    if not selected:
        raise WorkpaperExportError(
            f"{cycle.name} 没有可导出的预算版本（选择口径：{selection_label(selection)}）。",
            code="NO_EXPORTABLE_UPLOADS",
        )
    return selected, omitted


def preflight_cycle_workpapers(
    cycle: BudgetCycle | int,
    selection: str | None = SELECTION_APPROVED,
    *,
    project_ids: Sequence[int] | None = None,
) -> dict:
    cycle_obj = _cycle(cycle)
    normalized = normalize_selection(selection)
    if normalized == SELECTION_HISTORICAL:
        raise WorkpaperExportError(
            "批量导出只能选择当前批准版本或最新上传版本。",
            code="INVALID_BATCH_SELECTION",
        )
    issues: list[dict] = []
    projects = _project_queryset(cycle_obj, project_ids)
    if not projects:
        issues.append(
            {
                "project_code": "—",
                "project_name": "当前筛选",
                "code": "NO_PROJECTS",
                "message": "没有可导出的项目。",
            }
        )
    for project in projects:
        try:
            upload = select_upload(project, cycle_obj, normalized)
        except WorkpaperExportError as exc:
            issues.append(
                {
                    "project_code": project.code,
                    "project_name": project.name,
                    "code": exc.code,
                    "message": str(exc),
                }
            )
            continue
        for artifact in (
            ("original",)
            if cycle_obj.source_budget_year
            else ("original", "recalculated")
        ):
            availability = check_workpaper_file(upload, artifact)
            if not availability.available:
                issues.append(
                    {
                        "project_code": project.code,
                        "project_name": project.name,
                        "code": availability.code,
                        "message": availability.message,
                    }
                )
    return {
        "selection": normalized,
        "selection_label": selection_label(normalized),
        "ready": not issues,
        "projects_count": len(projects),
        "issues": issues,
    }


def build_cycle_workpaper_zip(
    cycle: BudgetCycle | int,
    selection: str | None = SELECTION_APPROVED,
    *,
    project_ids: Sequence[int] | None = None,
) -> WorkpaperArchive:
    cycle_obj = _cycle(cycle)
    normalized = normalize_selection(selection)
    if normalized == SELECTION_HISTORICAL:
        raise WorkpaperExportError(
            "批量导出只能选择当前批准版本或最新上传版本。",
            code="INVALID_BATCH_SELECTION",
        )
    uploads, omitted = _selected_for_batch(cycle_obj, normalized, project_ids)
    workpapers: list[tuple[WorkpaperFile, str]] = []
    for upload in uploads:
        for artifact in (
            ("original",)
            if cycle_obj.source_budget_year
            else ("original", "recalculated")
        ):
            workpaper = resolve_workpaper_file(upload, artifact)
            project_code = _safe_name(
                upload.project.code, f"project-{upload.project_id}"
            )
            archive_path = (
                f"{project_code}/{cycle_obj.budget_year}/"
                f"{project_code}-{str(upload.id)[:8]}-"
                f"{'original' if artifact == 'original' else 'recalculated'}{workpaper.path.suffix.lower()}"
            )
            workpapers.append((workpaper, archive_path))

    temp_dir = Path(tempfile.mkdtemp(prefix="budget-workpaper-export-"))
    archive_path = temp_dir / f"{_safe_name(cycle_obj.name, 'cycle')}-{normalized}.zip"
    manifest = {
        "schema_version": 1,
        "export_type": "budget_workpapers",
        "selection": normalized,
        "selection_label": selection_label(normalized),
        "cycle": {
            "id": cycle_obj.pk,
            "name": cycle_obj.name,
            "budget_year": cycle_obj.budget_year,
            "source_budget_year": cycle_obj.source_budget_year,
            "recalculated": not bool(cycle_obj.source_budget_year),
            "revision_no": cycle_obj.revision_no,
            "status": cycle_obj.status,
        },
        "generated_at": timezone.now().isoformat(),
        "files": [],
        "omitted_projects": omitted,
    }
    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            for workpaper, member_name in workpapers:
                archive.write(workpaper.path, arcname=member_name)
                manifest["files"].append(_manifest_file(workpaper, member_name))
            archive.writestr(
                "manifest.json",
                json.dumps(
                    manifest, ensure_ascii=False, indent=2, sort_keys=True
                ).encode("utf-8"),
            )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return WorkpaperArchive(path=archive_path, manifest=manifest)


def cleanup_workpaper_archive(path: Path | str) -> None:
    archive_path = Path(path)
    try:
        archive_path.unlink(missing_ok=True)
    finally:
        parent = archive_path.parent
        if parent.name.startswith("budget-workpaper-export-"):
            shutil.rmtree(parent, ignore_errors=True)


build_workpaper_zip = build_cycle_workpaper_zip
resolve_upload = select_upload
