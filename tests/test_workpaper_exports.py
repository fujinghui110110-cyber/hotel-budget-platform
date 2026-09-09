import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from budgeting.models import (
    AuditEvent,
    BudgetCycle,
    Project,
    ProjectCycle,
    UploadVersion,
    User,
)


class WorkpaperExportTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Path(self.temp.name)
        self.settings = override_settings(BUDGET_STORAGE_ROOT=self.storage)
        self.settings.enable()
        self.cycle = BudgetCycle.objects.create(
            name="2027预算 R1", budget_year=2027, status=BudgetCycle.Status.OPEN
        )
        self.project = Project.objects.create(code="P001", name="项目一")
        self.other_project = Project.objects.create(code="P002", name="项目二")
        self.admin = User.objects.create_user(
            username="admin", password="secret", role=User.Role.ADMIN
        )
        self.project_user = User.objects.create_user(
            username="project-user",
            password="secret",
            role=User.Role.PROJECT,
            project=self.project,
        )
        self.client = Client()

    def tearDown(self):
        self.settings.disable()
        self.temp.cleanup()

    def _file(self, relative, content):
        path = self.storage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _upload(
        self, project, name, status=UploadVersion.Status.APPROVED, *, content=None
    ):
        original = self._file(
            f"uploads/{project.code}/{name}-original.xlsx",
            content or f"original-{name}".encode(),
        )
        recalculated = self._file(
            f"uploads/{project.code}/{name}-recalculated.xlsx",
            f"recalculated-{name}".encode(),
        )
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        return UploadVersion.objects.create(
            project=project,
            cycle=self.cycle,
            status=status,
            original_name=f"{name}.xlsx",
            original_path=str(original.relative_to(self.storage)),
            recalculated_path=str(recalculated.relative_to(self.storage)),
            sha256=digest,
        )

    def _approved(self, project, name):
        upload = self._upload(project, name)
        ProjectCycle.objects.create(
            project=project, cycle=self.cycle, current_upload=upload
        )
        return upload

    def _login_admin(self):
        self.assertTrue(self.client.login(username="admin", password="secret"))

    def test_admin_downloads_current_approved_original_and_recalculated(self):
        upload = self._approved(self.project, "approved")
        self._login_admin()
        url = reverse(
            "management_workpaper_download", args=[self.project.pk, "original"]
        )
        response = self.client.get(url, {"cycle": self.cycle.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"original-approved")
        response.close()
        recalc_url = reverse(
            "management_workpaper_download", args=[self.project.pk, "recalculated"]
        )
        recalc_response = self.client.get(recalc_url, {"cycle": self.cycle.pk})
        self.assertEqual(recalc_response.status_code, 200)
        self.assertEqual(
            b"".join(recalc_response.streaming_content), b"recalculated-approved"
        )
        recalc_response.close()
        actions = set(
            AuditEvent.objects.filter(upload=upload).values_list("action", flat=True)
        )
        self.assertIn("WORKPAPER_ORIGINAL_DOWNLOADED", actions)
        self.assertIn("WORKPAPER_RECALCULATED_DOWNLOADED", actions)

    def test_latest_upload_requires_explicit_selection(self):
        approved = self._approved(self.project, "approved")
        latest = self._upload(
            self.project, "latest", status=UploadVersion.Status.SUBMITTED
        )
        self._login_admin()
        url = reverse(
            "management_workpaper_download", args=[self.project.pk, "original"]
        )
        default_response = self.client.get(url, {"cycle": self.cycle.pk})
        self.assertEqual(
            b"".join(default_response.streaming_content), b"original-approved"
        )
        default_response.close()
        latest_response = self.client.get(
            url, {"cycle": self.cycle.pk, "selection": "latest"}
        )
        self.assertEqual(
            b"".join(latest_response.streaming_content), b"original-latest"
        )
        latest_response.close()
        self.assertEqual(
            AuditEvent.objects.filter(
                upload=approved, action="WORKPAPER_ORIGINAL_DOWNLOADED"
            ).count(),
            1,
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                upload=latest, action="WORKPAPER_ORIGINAL_DOWNLOADED"
            ).count(),
            1,
        )

    def test_batch_zip_contains_both_files_and_selection_manifest(self):
        self._approved(self.project, "one")
        self._approved(self.other_project, "two")
        self._login_admin()
        url = reverse("management_workpaper_batch_download")
        response = self.client.get(url, {"cycle": self.cycle.pk})
        self.assertEqual(response.status_code, 200)
        payload = b"".join(response.streaming_content)
        response.close()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["selection"], "approved")
            self.assertEqual(len(manifest["files"]), 4)
            self.assertEqual(
                {item["status"] for item in manifest["files"]}, {"APPROVED"}
            )
            self.assertTrue(
                all(len(item["sha256"]) == 64 for item in manifest["files"])
            )
            self.assertNotIn("secret", archive.read("manifest.json").decode("utf-8"))

    def test_management_page_exposes_explicit_selection_links(self):
        self._approved(self.project, "one")
        self._login_admin()
        response = self.client.get(
            reverse("management_workpaper_exports"), {"cycle": self.cycle.pk}
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("当前批准版本 ZIP", body)
        self.assertIn("最新上传版本 ZIP", body)
        self.assertNotIn("WORKPAPER", body)

    def test_project_account_cannot_download_management_workpaper(self):
        upload = self._approved(self.project, "approved")
        self.assertTrue(self.client.login(username="project-user", password="secret"))
        url = reverse(
            "management_workpaper_download", args=[self.project.pk, "original"]
        )
        response = self.client.get(url, {"cycle": self.cycle.pk})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AuditEvent.objects.filter(upload=upload).exists())

    def test_missing_file_and_unsafe_path_are_clear_errors(self):
        upload = self._approved(self.project, "approved")
        self._login_admin()
        self.storage.joinpath(upload.original_path).unlink()
        url = reverse(
            "management_workpaper_download", args=[self.project.pk, "original"]
        )
        response = self.client.get(url, {"cycle": self.cycle.pk})
        self.assertEqual(response.status_code, 404)
        self.assertIn("文件不存在", response.content.decode("utf-8"))
        upload.original_path = "../outside.xlsx"
        upload.save(update_fields=["original_path"])
        unsafe_response = self.client.get(url, {"cycle": self.cycle.pk})
        self.assertEqual(unsafe_response.status_code, 404)
        self.assertIn("不安全", unsafe_response.content.decode("utf-8"))

    def test_management_page_preflights_missing_artifact_without_hashing(self):
        upload = self._approved(self.project, "approved")
        self.storage.joinpath(upload.recalculated_path).unlink()
        self._login_admin()
        with patch("budgeting.services.workpaper_exports.sha256_file") as digest:
            response = self.client.get(
                reverse("management_workpaper_exports"),
                {"cycle": self.cycle.pk, "project_id": self.project.pk},
            )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("重算 XLSX（不可用）", body)
        self.assertIn("当前批准版本 ZIP 不可用", body)
        digest.assert_not_called()

    def test_batch_missing_artifact_remains_a_clear_failure(self):
        upload = self._approved(self.project, "approved")
        self.storage.joinpath(upload.original_path).unlink()
        self._login_admin()
        response = self.client.get(
            reverse("management_workpaper_batch_download"),
            {"cycle": self.cycle.pk, "project_id": self.project.pk},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("文件不存在", response.content.decode("utf-8"))

    def test_history_links_pin_upload_id_and_download_selected_version(self):
        approved = self._approved(self.project, "approved")
        historical = self._upload(
            self.project, "historical", status=UploadVersion.Status.SUBMITTED
        )
        self._login_admin()
        response = self.client.get(
            reverse("management_workpaper_exports"),
            {"cycle": self.cycle.pk, "project_id": self.project.pk},
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("指定历史版本", body)
        self.assertIn(f"upload_id={historical.id}", body)
        self.assertIn(f"upload_id={approved.id}", body)
        url = reverse(
            "management_workpaper_download", args=[self.project.pk, "original"]
        )
        download = self.client.get(
            url,
            {
                "cycle": self.cycle.pk,
                "selection": "historical",
                "upload_id": str(historical.id),
            },
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"original-historical")
        download.close()
