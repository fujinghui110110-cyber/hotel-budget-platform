from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from budgeting.models import AuditEvent, BudgetCycle, ProcessingJob, Project, TemplateVersion, UploadVersion
from budgeting.services.batch_import import (
    MAX_BATCH_BYTES,
    MAX_BATCH_FILES,
    match_project_filename,
    preflight_batch,
)


TEST_MIDDLEWARE = [
    item for item in settings.MIDDLEWARE if item != "whitenoise.middleware.WhiteNoiseMiddleware"
]


@override_settings(MIDDLEWARE=TEST_MIDDLEWARE)
class BatchImportTests(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.project = Project.objects.create(code="P001", name="示例项目")
        self.other = Project.objects.create(code="P002", name="其他项目")
        self.cycle = BudgetCycle.objects.create(name="2027预算", budget_year=2027, status=BudgetCycle.OPEN)
        self.template = TemplateVersion.objects.create(
            version="V3-2027",
            budget_year=2027,
            file_path="template.xlsx",
            manifest_path="manifest.json",
            formula_manifest_hash="0" * 64,
        )
        self.admin = self.User.objects.create_user("batch_admin", password="x", role="ADMIN", is_staff=True)
        self.project_user = self.User.objects.create_user("batch_project", password="x", role="PROJECT", project=self.project)

    def file(self, name, body=b"fixture"):
        return SimpleUploadedFile(name, body)

    def test_project_account_cannot_open_batch_upload(self):
        self.client.force_login(self.project_user)

        response = self.client.get(reverse("management_batch_upload"))

        self.assertEqual(response.status_code, 403)

    def test_filename_match_supports_code_name_serial_and_year(self):
        matched_by_code = match_project_filename("01-2027年-P001-预算底稿.xlsx")
        matched_by_name = match_project_filename("2027年度 示例项目 预算套表.xlsx")

        self.assertEqual(matched_by_code, self.project)
        self.assertEqual(matched_by_name, self.project)

    def test_filename_match_rejects_ambiguous_project_name(self):
        Project.objects.create(code="P003", name="示例项目")

        with self.assertRaisesMessage(ValueError, "多个项目"):
            match_project_filename("2027年度 示例项目.xlsx")

    def test_filename_match_rejects_code_and_name_conflict(self):
        Project.objects.create(code="P003", name="示例项目")

        with self.assertRaisesMessage(ValueError, "多个项目"):
            match_project_filename("2027年度 P002 示例项目.xlsx")

    def test_filename_match_rejects_two_project_names_in_one_filename(self):
        Project.objects.create(code="FOUR", name="万宁福朋")
        Project.objects.create(code="CAREY", name="深圳凯骊酒店")

        with self.assertRaisesMessage(ValueError, "多个项目"):
            match_project_filename("万宁福朋-深圳凯骊酒店.xlsx")

    def test_filename_match_keeps_project_code_token_boundary(self):
        first = Project.objects.create(code="A1", name="一号短码")
        tenth = Project.objects.create(code="A10", name="十号短码")

        self.assertEqual(match_project_filename("2027-A1-预算.xlsx"), first)
        self.assertEqual(match_project_filename("2027-A10-预算.xlsx"), tenth)

    def test_preflight_rejects_duplicate_project_before_saving(self):
        preflight = preflight_batch(
            [self.file("P001-预算.xlsx"), self.file("序号2-示例项目.xlsx")],
            self.cycle,
        )

        self.assertFalse(preflight.valid)
        self.assertIn("重复出现", preflight.items[0].message)
        self.assertIn("重复出现", preflight.items[1].message)

    def test_preflight_uses_batch_limits_and_rejects_frozen_cycle(self):
        frozen = BudgetCycle.objects.create(name="2026预算", budget_year=2026, status=BudgetCycle.FROZEN)
        too_many = [self.file(f"P001-{index}.xlsx") for index in range(MAX_BATCH_FILES + 1)]
        oversized = self.file("P001.xlsx")
        oversized.size = MAX_BATCH_BYTES + 1

        frozen_preflight = preflight_batch([self.file("P001.xlsx")], frozen)
        count_preflight = preflight_batch(too_many, self.cycle)
        size_preflight = preflight_batch([oversized], self.cycle)

        self.assertFalse(frozen_preflight.valid)
        self.assertIn("不允许上传", " ".join(frozen_preflight.errors))
        self.assertIn(str(MAX_BATCH_FILES), " ".join(count_preflight.errors))
        self.assertIn(str(MAX_BATCH_BYTES // (1024 * 1024)), " ".join(size_preflight.errors))

    @override_settings(BUDGET_PROCESS_UPLOAD_INLINE=False)
    def test_admin_batch_upload_preflights_then_queues_each_file(self):
        upload_one = UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            template=self.template,
            original_name="P001.xlsx",
            original_path="mock/P001.xlsx",
            sha256="1" * 64,
        )
        upload_two = UploadVersion.objects.create(
            project=self.other,
            cycle=self.cycle,
            template=self.template,
            original_name="P002.xlsx",
            original_path="mock/P002.xlsx",
            sha256="2" * 64,
        )
        self.client.force_login(self.admin)

        with patch("budgeting.services.batch_import.save_upload", side_effect=[upload_one, upload_two]) as save_upload:
            response = self.client.post(
                reverse("management_batch_upload"),
                {
                    "cycle": str(self.cycle.pk),
                    "files": [self.file("2027-P001-预算.xlsx"), self.file("2027-P002-预算.xlsx")],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "已接收 2 个预算文件")
        self.assertContains(response, str(upload_one.pk))
        self.assertContains(response, str(upload_two.pk))
        self.assertEqual(save_upload.call_count, 2)
        self.assertEqual(ProcessingJob.objects.filter(upload__in=[upload_one, upload_two]).count(), 2)
        self.assertTrue(AuditEvent.objects.filter(action="BATCH_UPLOAD_PREFLIGHT", payload__valid=True).exists())
        self.assertEqual(AuditEvent.objects.filter(action="BATCH_UPLOAD_ENQUEUED").count(), 2)

    def test_admin_batch_upload_rejects_unmatched_without_saving(self):
        self.client.force_login(self.admin)

        with patch("budgeting.services.batch_import.save_upload") as save_upload:
            response = self.client.post(
                reverse("management_batch_upload"),
                {"cycle": str(self.cycle.pk), "files": [self.file("未知项目.xlsx")]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "未写入任何文件")
        self.assertContains(response, "文件名未匹配到项目代码或项目名称")
        save_upload.assert_not_called()
        self.assertTrue(AuditEvent.objects.filter(action="BATCH_UPLOAD_PREFLIGHT", payload__valid=False).exists())

    def test_admin_batch_upload_handles_invalid_cycle_without_500(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("management_batch_upload"),
            {"cycle": "abc", "files": [self.file("P001.xlsx")]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "请选择一个预算版本")

    @override_settings(BUDGET_PROCESS_UPLOAD_INLINE=False)
    def test_admin_batch_upload_reports_zero_enqueued_as_error(self):
        self.client.force_login(self.admin)

        with patch("budgeting.services.batch_import.save_upload", side_effect=OSError("disk full")):
            response = self.client.post(
                reverse("management_batch_upload"),
                {"cycle": str(self.cycle.pk), "files": [self.file("P001.xlsx")]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "本批文件未能加入队列")
        self.assertContains(response, "disk full")
        self.assertFalse(AuditEvent.objects.filter(action="BATCH_UPLOAD_ENQUEUED").exists())
        self.assertTrue(AuditEvent.objects.filter(action="BATCH_UPLOAD_FAILED").exists())
