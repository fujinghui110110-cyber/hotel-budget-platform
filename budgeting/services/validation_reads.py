from django.db.models import F, OuterRef, Q, Subquery

from budgeting.models import ValidationIssue, ValidationRun


def current_validation_issues():
    latest_legacy_run = ValidationRun.objects.filter(
        upload_id=OuterRef("run__upload_id")
    ).order_by("-created_at", "-pk").values("pk")[:1]
    return ValidationIssue.objects.filter(
        Q(run__processing_run_id=F("run__upload__processing_current_run_id"))
        | Q(run__upload__processing_current_run__isnull=True, run_id=Subquery(latest_legacy_run))
    )
