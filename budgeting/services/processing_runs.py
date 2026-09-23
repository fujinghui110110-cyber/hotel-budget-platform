import time

from django.db import OperationalError, connection, transaction
from django.db.models import F
from django.utils import timezone

from budgeting.models import AuditEvent, BudgetCycle, ProcessingJob, ProcessingRun, UploadVersion, ValidationIssue, ValidationRun



def _rule_version(upload):
    return upload.template.rule_version if upload.template else "rules-v1"


def _is_sqlite_locked(exc):
    return connection.vendor == "sqlite" and "locked" in str(exc).lower()


def _actor_can_retry(actor, upload):
    if actor is None:
        return True
    if getattr(actor, "is_admin_role", False):
        return True
    role_class = getattr(actor, "Role", None)
    project_role = role_class is not None and getattr(actor, "role", None) == getattr(role_class, "PROJECT", None)
    return project_role and getattr(actor, "project_id", None) == upload.project_id


def _serialize_upload_queue(upload_id):
    if connection.vendor == "sqlite":
        UploadVersion.objects.filter(pk=upload_id).update(note=F("note"))


def enqueue_processing_job(upload, *, actor=None, reason=""):
    for attempt in range(25):
        try:
            return _enqueue_processing_job_once(upload, actor=actor, reason=reason)
        except OperationalError as exc:
            if not _is_sqlite_locked(exc) or attempt == 24:
                raise
            time.sleep(0.02 * (attempt + 1))


@transaction.atomic
def _enqueue_processing_job_once(upload, *, actor=None, reason=""):
    if connection.vendor == "sqlite":
        _serialize_upload_queue(upload.pk)
        upload = UploadVersion.objects.select_related("cycle", "template", "project").get(pk=upload.pk)
    else:
        upload = UploadVersion.objects.select_for_update().select_related("cycle", "template", "project").get(pk=upload.pk)
    if not _actor_can_retry(actor, upload):
        raise ValueError("无权重排该上传的处理任务")
    if upload.cycle.status == BudgetCycle.Status.FROZEN:
        raise ValueError("冻结周期不允许重排上传处理任务")
    retryable_statuses = {
        UploadVersion.Status.RECEIVED,
        UploadVersion.Status.PROCESSING,
        UploadVersion.Status.REJECTED,
    }
    if upload.status not in retryable_statuses:
        raise ValueError("已校验、已提交或正式版本不能重排同一上传；请提交新的上传版本")
    active = (
        ProcessingJob.objects
        .filter(upload=upload, status__in=[ProcessingJob.Status.QUEUED, ProcessingJob.Status.RUNNING])
        .order_by("-created_at")
        .first()
    )
    if active:
        return active, False
    run = ProcessingRun.objects.create(
        upload=upload,
        status=ProcessingRun.Status.QUEUED,
        rule_version=_rule_version(upload),
    )
    job = ProcessingJob.objects.create(
        upload=upload,
        processing_run=run,
        idempotency_key=f"upload:{upload.pk}:run:{run.pk}",
    )
    if upload.status == UploadVersion.Status.REJECTED:
        upload.status = UploadVersion.Status.RECEIVED
        upload.note = ""
        upload.save(update_fields=["status", "note"])
    AuditEvent.objects.create(
        actor=actor,
        action="PROCESSING_JOB_QUEUED",
        project=upload.project,
        cycle=upload.cycle,
        upload=upload,
        payload={
            "object_type": "ProcessingRun",
            "object_id": str(run.pk),
            "reason": reason,
            "job_id": job.pk,
        },
    )
    return job, True


def ensure_job_run(job):
    for attempt in range(25):
        try:
            return _ensure_job_run_once(job)
        except OperationalError as exc:
            if not _is_sqlite_locked(exc) or attempt == 24:
                raise
            time.sleep(0.02 * (attempt + 1))


def _ensure_job_run_once(job):
    job = ProcessingJob.objects.select_related("upload", "processing_run").get(pk=job.pk)
    if job.processing_run_id:
        return job.processing_run
    with transaction.atomic():
        job = ProcessingJob.objects.select_related("upload", "processing_run").get(pk=job.pk)
        if job.processing_run_id:
            return job.processing_run
        run = ProcessingRun.objects.create(
            upload=job.upload,
            status=ProcessingRun.Status.QUEUED,
            rule_version=_rule_version(job.upload),
        )
        claimed = ProcessingJob.objects.filter(pk=job.pk, processing_run__isnull=True).update(processing_run=run)
        if claimed:
            job.processing_run = run
            job.processing_run_id = run.pk
            return run
        run.delete()
        job = ProcessingJob.objects.select_related("processing_run").get(pk=job.pk)
        return job.processing_run


def mark_run_running(run):
    ProcessingRun.objects.filter(pk=run.pk).update(status=ProcessingRun.Status.RUNNING, started_at=timezone.now(), error="")
    run.status = ProcessingRun.Status.RUNNING
    run.started_at = timezone.now()
    run.error = ""
    return run


def mark_run_failed(run, error=""):
    ProcessingRun.objects.filter(pk=run.pk).update(
        status=ProcessingRun.Status.FAILED,
        error=str(error or ""),
        completed_at=timezone.now(),
    )


def mark_run_succeeded(run):
    ProcessingRun.objects.filter(pk=run.pk).update(
        status=ProcessingRun.Status.SUCCEEDED,
        error="",
        completed_at=timezone.now(),
    )


def current_validation_runs(upload):
    upload = UploadVersion.objects.filter(pk=upload.pk).only("pk", "processing_current_run_id").first()
    if upload is None:
        return ValidationRun.objects.none()
    if upload.processing_current_run_id:
        return ValidationRun.objects.filter(processing_run_id=upload.processing_current_run_id)
    latest = ValidationRun.objects.filter(upload=upload).order_by("-created_at", "-pk").first()
    if latest:
        return ValidationRun.objects.filter(pk=latest.pk)
    return ValidationRun.objects.none()


def current_blocking_issues(upload, severity):
    return ValidationIssue.objects.filter(run__in=current_validation_runs(upload), severity=severity)


def has_current_p0(upload):
    return current_blocking_issues(upload, ValidationIssue.Severity.P0).exists()


def has_unacknowledged_current_p1(upload):
    return any(
        not issue.acknowledged or not issue.acknowledgement_note.strip()
        for issue in current_blocking_issues(upload, ValidationIssue.Severity.P1)
    )
