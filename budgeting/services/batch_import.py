"""Helpers for the management-side multi-file budget workbook import.

The import deliberately has no database model of its own.  A batch is a
short-lived request concern; every accepted workbook is represented by the
existing ``UploadVersion`` and ``ProcessingJob`` records.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from django.conf import settings

from budgeting.forms import UploadForm
from budgeting.models import (
    AuditEvent,
    BudgetCycle,
    ProcessingJob,
    Project,
    ProjectCycle,
    UploadVersion,
)
from budgeting.services.budget_versions import UPLOADABLE_CYCLE_STATUSES
from budgeting.services.workflow import audit, process_upload_now, save_upload


# The existing single-file contract remains the source of truth for file type
# and size.  These limits protect the request itself from an accidental large
# selection while leaving room for several normal 50 MiB workbooks.
MAX_BATCH_FILES = 100
MAX_BATCH_BYTES = 500 * 1024 * 1024

MATCHED = "MATCHED"
AMBIGUOUS = "AMBIGUOUS"
UNMATCHED = "UNMATCHED"

BATCH_UPLOAD_PREFLIGHT = "BATCH_UPLOAD_PREFLIGHT"
BATCH_UPLOAD_ENQUEUED = "BATCH_UPLOAD_ENQUEUED"
BATCH_UPLOAD_FAILED = "BATCH_UPLOAD_FAILED"

_EXTENSION_RE = re.compile(r"\.(?:xlsx|xlsm)$", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20|21)\d{2}(?:年|年度)?")
_LEADING_SERIAL_RE = re.compile(
    r"^[\s_\-.—–·,，/\\()（）\[\]【】]*"
    r"(?:(?:序号|编号|no|number)\s*[.:：]?\s*)?"
    r"\d{1,4}(?=$|[\s_\-.—–·,，/\\()（）\[\]【】]+)",
    re.IGNORECASE,
)
_TRAILING_SERIAL_RE = re.compile(
    r"(?:[\s_\-.—–·,，/\\()（）\[\]【】]+"
    r"(?:(?:序号|编号|no|number)\s*[.:：]?\s*)?"
    r"\d{1,4})\s*$",
    re.IGNORECASE,
)
_SEPARATOR_RE = re.compile(r"[\s_\-.—–·,，/\\|:：;；]+")
_TOKEN_RE = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+")


def _filename_only(filename: object) -> str:
    """Return a browser-safe basename for either slash convention."""

    value = str(filename or "").replace("\\", "/")
    return value.rsplit("/", 1)[-1]


def _display_stem(filename: object) -> str:
    basename = _filename_only(filename)
    return _EXTENSION_RE.sub("", basename)


def _remove_serial_edges(value: str) -> str:
    # Serial numbers are only removed at an edge and only when separated from
    # the project label.  This preserves codes such as ``DEMO01``.
    previous = None
    while value != previous:
        previous = value
        value = _LEADING_SERIAL_RE.sub("", value, count=1).strip()
        value = _TRAILING_SERIAL_RE.sub("", value, count=1).strip()
    return value


def clean_project_filename(filename: object) -> str:
    """Normalize a workbook stem for project matching.

    The returned value is still human-readable.  It removes only workbook
    extensions, separated leading/trailing serials and standalone year tags;
    project names and codes are otherwise left intact.
    """

    value = unicodedata.normalize("NFKC", _display_stem(filename)).strip()
    value = _YEAR_RE.sub(" ", value)
    value = _remove_serial_edges(value)
    value = _SEPARATOR_RE.sub(" ", value)
    return value.strip(" .-_—–·,，/\\|:：;；()（）[]【】")


def _fold_match(value: object) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # ``isalnum`` keeps Chinese characters and letters/digits in project codes
    # while ignoring the harmless separators introduced by workbook naming.
    return "".join(character for character in value if character.isalnum())


def _fold_tokens(value: object) -> set[str]:
    return {_fold_match(match.group(0)) for match in _TOKEN_RE.finditer(str(value or ""))}


def _project_has_evidence(filename: str, project: Project) -> bool:
    stem = clean_project_filename(filename)
    cleaned = _fold_match(stem)
    raw = _fold_match(_display_stem(filename))
    tokens = _fold_tokens(stem) | _fold_tokens(_display_stem(filename))
    if not cleaned:
        return False

    code = _fold_match(getattr(project, "code", ""))
    if code and len(code) >= 2 and (cleaned == code or raw == code or code in tokens):
        return True

    name = _fold_match(getattr(project, "name", ""))
    if not name or len(name) < 2:
        return False
    return cleaned == name or raw == name or name in cleaned


@dataclass(frozen=True)
class FilenameMatch:
    """The explainable result of matching one workbook name to a project."""

    filename: str
    cleaned_stem: str
    status: str
    project: Project | None = None
    candidates: tuple[Project, ...] = ()
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.status == MATCHED and self.project is not None

    @property
    def candidate_labels(self) -> tuple[str, ...]:
        return tuple(f"{project.code} {project.name}" for project in self.candidates)


def explain_project_filename_match(
    filename: object,
    projects: Iterable[Project] | None = None,
) -> FilenameMatch:
    basename = _filename_only(filename)
    stem = clean_project_filename(basename)
    available = list(
        projects
        if projects is not None
        else Project.objects.filter(is_active=True).order_by("code")
    )
    candidates = tuple(
        sorted(
            (
                project
                for project in available
                if _project_has_evidence(basename, project)
            ),
            key=lambda project: (project.code.casefold(), project.pk or 0),
        )
    )
    if not candidates:
        return FilenameMatch(
            filename=basename,
            cleaned_stem=stem,
            status=UNMATCHED,
            reason="文件名未匹配到项目代码或项目名称。",
        )

    if len(candidates) != 1:
        labels = "、".join(f"{project.code} {project.name}" for project in candidates)
        return FilenameMatch(
            filename=basename,
            cleaned_stem=stem,
            status=AMBIGUOUS,
            candidates=candidates,
            reason=f"文件名可匹配多个项目：{labels}。请修改文件名后再上传。",
        )

    project = candidates[0]
    return FilenameMatch(
        filename=basename,
        cleaned_stem=stem,
        status=MATCHED,
        project=project,
        candidates=candidates,
        reason=f"已识别为 {project.code} {project.name}。",
    )


def match_project_filename(
    filename: object,
    projects: Iterable[Project] | None = None,
) -> Project:
    match = explain_project_filename_match(filename, projects)
    if match.matched and match.project is not None:
        return match.project
    raise ValueError(match.reason)


def _form_errors(form: UploadForm) -> list[str]:
    messages: list[str] = []
    for errors in form.errors.as_data().values():
        messages.extend(str(error) for error in errors)
    return messages or ["文件不符合上传要求。"]


@dataclass
class BatchFileCheck:
    """One preflight row, retaining the upload object for the enqueue pass."""

    position: int
    uploaded_file: object
    filename: str
    size: int
    match: FilenameMatch
    errors: list[str] = field(default_factory=list)
    project: Project | None = None

    @property
    def valid(self) -> bool:
        return not self.errors and self.match.matched and self.project is not None

    @property
    def status(self) -> str:
        if self.valid:
            return "READY"
        if self.match.status == AMBIGUOUS:
            return AMBIGUOUS
        if self.match.status == UNMATCHED:
            return UNMATCHED
        return "INVALID"

    @property
    def message(self) -> str:
        return "；".join(self.errors) if self.errors else self.match.reason


@dataclass
class BatchPreflight:
    cycle: BudgetCycle | None
    items: list[BatchFileCheck]
    errors: list[str] = field(default_factory=list)
    total_bytes: int = 0
    batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def valid(self) -> bool:
        return bool(self.cycle) and not self.errors and bool(self.items) and all(
            item.valid for item in self.items
        )

    @property
    def total_size_mb(self) -> float:
        return round(self.total_bytes / (1024 * 1024), 2)


def preflight_batch(
    files: Sequence[object] | Iterable[object] | None,
    cycle: BudgetCycle | None,
    *,
    projects: Iterable[Project] | None = None,
) -> BatchPreflight:
    """Validate all files and all project matches before writing any file."""

    selected = list(files or [])
    result = BatchPreflight(cycle=cycle, items=[])
    if cycle is None:
        result.errors.append("请选择一个预算版本。")
    elif cycle.status not in UPLOADABLE_CYCLE_STATUSES:
        result.errors.append("所选预算版本当前不允许上传。")

    result.total_bytes = sum(int(getattr(upload, "size", 0) or 0) for upload in selected)
    if not selected:
        result.errors.append("请至少选择一个预算文件。")
    if len(selected) > MAX_BATCH_FILES:
        result.errors.append(f"一次最多选择 {MAX_BATCH_FILES} 个文件。")
    if result.total_bytes > MAX_BATCH_BYTES:
        result.errors.append(
            f"本批文件总量不能超过 {MAX_BATCH_BYTES // (1024 * 1024)} MiB。"
        )

    available_projects = list(
        projects
        if projects is not None
        else Project.objects.filter(is_active=True).order_by("code")
    )
    for position, uploaded_file in enumerate(selected, 1):
        filename = _filename_only(getattr(uploaded_file, "name", ""))
        match = explain_project_filename_match(filename, available_projects)
        item = BatchFileCheck(
            position=position,
            uploaded_file=uploaded_file,
            filename=filename,
            size=int(getattr(uploaded_file, "size", 0) or 0),
            match=match,
            project=match.project,
        )
        form = UploadForm(files={"file": uploaded_file}, cycle=cycle)
        if not form.is_valid():
            item.errors.extend(_form_errors(form))
        if match.status == AMBIGUOUS:
            item.errors.append(match.reason)
        elif match.status == UNMATCHED:
            item.errors.append(match.reason)
        if match.project is not None and cycle is not None:
            project_cycle = ProjectCycle.objects.filter(
                project=match.project, cycle=cycle
            ).first()
            if project_cycle is not None and not project_cycle.is_open:
                item.errors.append(
                    f"项目 {match.project.code} 在所选预算版本中已关闭，不能上传。"
                )
        result.items.append(item)

    # Duplicate projects are a batch-level error.  Mark every colliding row so
    # the administrator sees the complete problem set before any write.
    by_project: dict[int, list[BatchFileCheck]] = {}
    for item in result.items:
        if item.project is not None and item.match.matched:
            by_project.setdefault(item.project.pk, []).append(item)
    for duplicate_items in by_project.values():
        if len(duplicate_items) < 2:
            continue
        project = duplicate_items[0].project
        names = "、".join(item.filename for item in duplicate_items)
        message = f"项目 {project.code} 在本批次重复出现（{names}），请每个项目只保留一个文件。"
        for item in duplicate_items:
            item.errors.append(message)

    return result


@dataclass
class BatchUploadItemResult:
    check: BatchFileCheck
    upload: UploadVersion | None = None
    job: ProcessingJob | None = None
    error: str = ""
    accepted: bool = False

    @property
    def project(self) -> Project | None:
        return self.check.project

    @property
    def status(self) -> str:
        if self.error:
            return "FAILED"
        if self.job is not None:
            return self.job.get_status_display()
        if self.upload is not None:
            return self.upload.get_status_display()
        return "未处理"


@dataclass
class BatchUploadOutcome:
    batch_id: str
    cycle: BudgetCycle
    items: list[BatchUploadItemResult]


def record_batch_preflight(preflight: BatchPreflight, actor=None) -> None:
    """Leave an audit record for both accepted and rejected preflight runs."""

    if preflight.cycle is None:
        return
    audit(
        actor,
        BATCH_UPLOAD_PREFLIGHT,
        "BudgetCycle",
        preflight.cycle.pk,
        {
            "batch_id": preflight.batch_id,
            "valid": preflight.valid,
            "file_count": len(preflight.items),
            "total_bytes": preflight.total_bytes,
            "errors": list(preflight.errors),
            "files": [
                {
                    "name": item.filename,
                    "status": item.status,
                    "project": item.project.code if item.project else "",
                    "errors": list(item.errors),
                }
                for item in preflight.items
            ],
        },
        cycle=preflight.cycle,
    )


def enqueue_batch_upload(
    preflight: BatchPreflight,
    *,
    actor=None,
    process_inline: bool | None = None,
) -> BatchUploadOutcome:
    """Persist every preflight-approved file through the normal upload path."""

    if not preflight.valid or preflight.cycle is None:
        raise ValueError("批量上传未通过完整预检，未写入任何文件。")

    record_batch_preflight(preflight, actor=actor)
    inline = settings.BUDGET_PROCESS_UPLOAD_INLINE if process_inline is None else process_inline
    outcome_items: list[BatchUploadItemResult] = []
    for item in preflight.items:
        result = BatchUploadItemResult(check=item)
        try:
            uploaded_file = item.uploaded_file
            seek = getattr(uploaded_file, "seek", None)
            if seek is not None:
                seek(0)
            upload = save_upload(item.project, preflight.cycle, uploaded_file)
            result.upload = upload
            if inline:
                result.job = process_upload_now(upload)
            else:
                result.job = ProcessingJob.objects.get_or_create(
                    upload=upload,
                    defaults={"idempotency_key": f"upload:{upload.pk}"},
                )[0]
            result.accepted = True
            audit(
                actor,
                BATCH_UPLOAD_ENQUEUED,
                "UploadVersion",
                upload.pk,
                {
                    "batch_id": preflight.batch_id,
                    "file": item.filename,
                    "project_code": item.project.code,
                    "job_status": result.job.status if result.job else "",
                },
                project=item.project,
                cycle=preflight.cycle,
                upload=upload,
            )
        except Exception as exc:  # keep the remaining files visible per row
            result.error = str(exc) or "文件入队失败。"
            audit(
                actor,
                BATCH_UPLOAD_FAILED,
                "UploadVersion",
                preflight.batch_id,
                {
                    "batch_id": preflight.batch_id,
                    "file": item.filename,
                    "project_code": item.project.code if item.project else "",
                    "error": result.error,
                },
                project=item.project,
                cycle=preflight.cycle,
            )
        outcome_items.append(result)
    return BatchUploadOutcome(
        batch_id=preflight.batch_id,
        cycle=preflight.cycle,
        items=outcome_items,
    )


# Short aliases make the two independently reusable service stages easy to
# discover for callers that prefer ``preview``/``enqueue`` terminology.
preview_batch = preflight_batch
enqueue_batch = enqueue_batch_upload


__all__ = [
    "AMBIGUOUS",
    "BATCH_UPLOAD_ENQUEUED",
    "BATCH_UPLOAD_FAILED",
    "BATCH_UPLOAD_PREFLIGHT",
    "BatchFileCheck",
    "BatchPreflight",
    "BatchUploadItemResult",
    "BatchUploadOutcome",
    "FilenameMatch",
    "MATCHED",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_FILES",
    "UNMATCHED",
    "clean_project_filename",
    "enqueue_batch",
    "enqueue_batch_upload",
    "explain_project_filename_match",
    "match_project_filename",
    "preflight_batch",
    "preview_batch",
    "record_batch_preflight",
]
