from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from budgeting.models import (
    BudgetCycle, ProcessingJob, ProcessingRun, Project, TemplateVersion,
    UploadVersion, ValidationIssue, ValidationRun,
)
from budgeting.services.validation_reads import current_validation_issues


@override_settings(BUDGET_PROCESS_UPLOAD_INLINE=False)
class ProcessingRetryViewTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(code="RETRY", name="重试项目")
        self.other = Project.objects.create(code="OTHER", name="其他项目")
        self.cycle = BudgetCycle.objects.create(name="2027 R1", budget_year=2027, status="OPEN")
        self.template = TemplateVersion.objects.create(
            budget_year=2027, file_path="unused.xlsx", manifest_path="unused.json",
            formula_manifest_hash="0" * 64,
        )
        self.upload = UploadVersion.objects.create(
            project=self.project, cycle=self.cycle, template=self.template,
            original_path="original.xlsx", sha256="1" * 64, status="REJECTED",
        )
        self.job = ProcessingJob.objects.create(upload=self.upload, status="FAILED", idempotency_key="retry-original")
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", role="PROJECT", project=self.project)
        self.outsider = User.objects.create_user(username="outsider", role="PROJECT", project=self.other)
        self.admin = User.objects.create_user(username="admin-retry", role="ADMIN")
        self.url = reverse("upload_retry", args=[self.upload.pk])

    def test_owner_retry_is_post_only_and_double_click_does_not_duplicate(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertEqual(ProcessingJob.objects.filter(upload=self.upload, status="QUEUED").count(), 1)
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.original_path, "original.xlsx")
        self.assertEqual(self.upload.sha256, "1" * 64)

    def test_other_project_cannot_retry(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertEqual(ProcessingJob.objects.filter(upload=self.upload).count(), 1)

    def test_admin_can_retry(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertEqual(ProcessingJob.objects.filter(upload=self.upload).count(), 2)

    def test_validation_rejection_is_not_infrastructure_retry(self):
        self.job.status = "DONE"
        self.job.save(update_fields=["status"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertEqual(ProcessingJob.objects.filter(upload=self.upload).count(), 1)

    def test_current_issue_query_ignores_old_failure_and_preserves_other_upload(self):
        old = ValidationRun.objects.create(upload=self.upload, rule_version="v1")
        stale = ValidationIssue.objects.create(run=old, severity="P0", code="OLD", message="旧错误")
        run = ProcessingRun.objects.create(upload=self.upload, status="SUCCEEDED")
        current = ValidationRun.objects.create(upload=self.upload, processing_run=run, rule_version="v1", passed=True)
        active = ValidationIssue.objects.create(run=current, severity="P2", code="NEW", message="本次提示")
        self.upload.processing_current_run = run
        self.upload.save(update_fields=["processing_current_run"])
        self.assertEqual(set(current_validation_issues().values_list("pk", flat=True)), {active.pk})
        self.assertTrue(ValidationIssue.objects.filter(pk=stale.pk).exists())
        # Legacy uploads also use the latest validation, even when it has no issues.
        self.upload.processing_current_run = None
        self.upload.save(update_fields=["processing_current_run"])
        ValidationRun.objects.create(upload=self.upload, rule_version="v1", passed=True)
        self.assertFalse(current_validation_issues().exists())
