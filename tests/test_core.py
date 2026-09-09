import tempfile
import zipfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase

from budgeting.models import BudgetCycle, NormalizedValue, Project, ProjectCycle, TemplateVersion, UploadVersion
from budgeting.services.allocations import largest_remainder
from budgeting.services.files import check_xlsx_package
from budgeting.services.money import aggregate_occ, yuan_to_cents
from budgeting.services.workflow import approve_upload, freeze_preconditions, submit_upload


class CalculationTests(TestCase):
    def test_money_round_half_up(self):
        self.assertEqual(yuan_to_cents("1.235"), 124)
        self.assertEqual(yuan_to_cents("-1.235"), -124)

    def test_largest_remainder_positive_and_negative(self):
        items = [("P001", 0), ("P002", 0), ("P003", 0)]
        self.assertEqual(largest_remainder(10000, items), [("P001", 3334), ("P002", 3333), ("P003", 3333)])
        self.assertEqual(largest_remainder(-10000, items), [("P001", -3334), ("P002", -3333), ("P003", -3333)])

    def test_occ_aggregates_numerator_denominator(self):
        num, den, ratio = aggregate_occ([(1, 2), (9, 18)])
        self.assertEqual((num, den), (10, 20))
        self.assertEqual(str(ratio), "0.5")


class SecurityTests(TestCase):
    def test_zip_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.xlsx"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("../evil", "x")
            with self.assertRaises(ValidationError):
                check_xlsx_package(path)

    def test_zip_blocks_external_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.xlsx"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("[Content_Types].xml", "")
                zf.writestr("xl/externalLinks/externalLink1.xml", "")
            with self.assertRaises(ValidationError):
                check_xlsx_package(path)


class WorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.project = Project.objects.create(code="P001", name="项目一")
        self.other = Project.objects.create(code="P002", name="项目二")
        self.cycle = BudgetCycle.objects.create(name="2026", budget_year=2026, status=BudgetCycle.Status.OPEN)
        self.template = TemplateVersion.objects.create(version="V1", budget_year=2026, file_path="x.xlsx", manifest_path="m.json", formula_manifest_hash="0" * 64)
        self.admin = User.objects.create_user("admin", password="x", role="ADMIN", is_staff=True)
        self.user = User.objects.create_user("p001", password="x", role="PROJECT", project=self.project)
        self.other_user = User.objects.create_user("p002", password="x", role="PROJECT", project=self.other)
        self.upload = UploadVersion.objects.create(project=self.project, cycle=self.cycle, template=self.template, status=UploadVersion.Status.VALIDATED, original_path="original.xlsx", sha256="a" * 64)

    def test_submit_then_approve_switches_current_version(self):
        submit_upload(self.upload, self.user)
        approve_upload(self.upload, self.admin)
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadVersion.Status.APPROVED)
        self.assertEqual(ProjectCycle.objects.get(project=self.project, cycle=self.cycle).current_upload, self.upload)

    def test_project_upload_url_is_scoped(self):
        client = Client()
        client.force_login(self.other_user)
        response = client.get(f"/project/uploads/{self.upload.id}/")
        self.assertEqual(response.status_code, 403)

    def test_freeze_requires_all_current_versions(self):
        submit_upload(self.upload, self.user)
        approve_upload(self.upload, self.admin)
        self.assertIn("P002 无正式版本", freeze_preconditions(self.cycle))

    def test_normalized_value_unique_key(self):
        NormalizedValue.objects.create(upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R1", period="01", unit="MONEY", value_int=1, source_sheet="s", source_cell="A1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NormalizedValue.objects.create(upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R1", period="01", unit="MONEY", value_int=2, source_sheet="s", source_cell="A1")
