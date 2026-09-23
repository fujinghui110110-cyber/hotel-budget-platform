import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from budgeting.models import BudgetCycle, ProcessingJob, Project, UploadVersion, User


class HostedUploadTests(TestCase):
    @override_settings(DEBUG=False)
    def test_production_refuses_weak_demo_accounts(self):
        for command in ("init_demo", "seed_demo", "seed_planning_demo"):
            with self.subTest(command=command), self.assertRaises(CommandError):
                call_command(command)

    @override_settings(BUDGET_PROCESS_UPLOAD_INLINE=False)
    def test_upload_returns_queued_without_running_excel_in_web_request(self):
        project = Project.objects.create(code="HOST01", name="队列验收项目")
        user = User.objects.create_user(username="host_project", project=project)
        cycle = BudgetCycle.objects.create(name="2028预算", budget_year=2028, status="OPEN")
        upload = UploadVersion.objects.create(project=project, cycle=cycle, original_path="not-read.xlsx", sha256="0" * 64)
        job = ProcessingJob.objects.create(upload=upload, idempotency_key=f"upload:{upload.pk}")
        self.client.force_login(user)
        with patch("budgeting.views.save_upload", return_value=upload), patch("budgeting.views.process_upload_now") as inline:
            response = self.client.post(reverse("project_upload_new"), {"file": SimpleUploadedFile("budget.xlsx", b"fixture")})
        inline.assert_not_called()
        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingJob.Status.QUEUED)
        messages = " ".join(str(message) for message in get_messages(response.wsgi_request))
        self.assertIn("正在运行", messages)
        self.assertNotIn("完成校验", messages)
        detail = self.client.get(response.url)
        self.assertContains(detail, 'data-status-url=')
        self.assertContains(detail, "完成后本页会自动刷新")

    def test_production_configuration_rejects_shared_default_secret(self):
        env = {**os.environ, "DJANGO_DEBUG": "0", "DJANGO_SECRET_KEY": "local-mvp-change-before-network-deploy", "DJANGO_ALLOWED_HOSTS": "budget.example.com"}
        result = subprocess.run([sys.executable, "-c", "import config.settings"], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
