import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.conf import settings
from django.db import OperationalError, connection
from django.db.models import F
from django.utils import timezone

from budgeting.models import ProcessingJob, UploadVersion
from budgeting.services.workflow import InfrastructureProcessingError, process_upload


class Command(BaseCommand):
    help = "Run the single-concurrency local budget worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--sleep", type=float, default=2)
        parser.add_argument("--lease-seconds", type=int, default=240)

    def handle(self, *args, **options):
        while True:
            if (settings.BASE_DIR / '.runtime/update-maintenance').exists():
                if options["once"]:
                    break
                time.sleep(options["sleep"])
                continue
            job = self._claim_job(options["lease_seconds"])
            if job:
                self._run_job(job)
            elif options["once"]:
                return
            else:
                time.sleep(options["sleep"])

    def _claim_job(self, lease_seconds):
        for attempt in range(5):
            try:
                return self._claim_once(lease_seconds)
            except OperationalError as exc:
                if connection.vendor != "sqlite" or "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))

    def _claim_once(self, lease_seconds):
        now = timezone.now()
        expired = ProcessingJob.objects.filter(
            status=ProcessingJob.Status.RUNNING,
            lease_until__lt=now,
        )
        expired.update(status=ProcessingJob.Status.QUEUED, lease_until=None)

        job = (
            ProcessingJob.objects
            .filter(status=ProcessingJob.Status.QUEUED)
            .order_by("created_at")
            .first()
        )
        if not job:
            return None
        claimed = ProcessingJob.objects.filter(
            pk=job.pk,
            status=ProcessingJob.Status.QUEUED,
        ).update(
            status=ProcessingJob.Status.RUNNING,
            attempts=F("attempts") + 1,
            lease_until=now + timedelta(seconds=lease_seconds),
            heartbeat_at=now,
        )
        if not claimed:
            return None
        job.status = ProcessingJob.Status.RUNNING
        job.attempts += 1
        job.lease_until = now + timedelta(seconds=lease_seconds)
        job.heartbeat_at = now
        return job

    def _run_job(self, job):
        job = ProcessingJob.objects.get(pk=job.pk)
        if job.status != ProcessingJob.Status.RUNNING:
            return job
        upload = job.upload
        upload.status = UploadVersion.Status.PROCESSING
        upload.save(update_fields=["status"])
        try:
            process_upload(upload)
        except InfrastructureProcessingError as exc:
            if job.attempts < 2:
                job.status = ProcessingJob.Status.QUEUED
                job.error = str(exc)
            else:
                upload.status = UploadVersion.Status.REJECTED
                upload.note = str(exc)
                upload.save(update_fields=["status", "note"])
                job.status = ProcessingJob.Status.FAILED
                job.error = str(exc)
        except Exception as exc:
            upload.status = UploadVersion.Status.REJECTED
            upload.note = str(exc)
            upload.save(update_fields=["status", "note"])
            job.status = ProcessingJob.Status.FAILED
            job.error = str(exc)
        else:
            from budgeting.services.data_read_audit import build_upload_audit
            try:
                build_upload_audit(upload)
            except Exception as exc:
                self.stderr.write(f"数据读取审计暂不可用（{type(exc).__name__}），查看审计页面时将重试。")
            job.status = ProcessingJob.Status.DONE
            job.error = ""
        job.lease_until = None
        job.heartbeat_at = timezone.now()
        job.save(update_fields=["status", "error", "lease_until", "heartbeat_at"])
        return job
