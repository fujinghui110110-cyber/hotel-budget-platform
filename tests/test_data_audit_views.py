from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from budgeting.models import BudgetCycle, Project, UploadVersion, User


class DataAuditViewsTests(TestCase):
    def setUp(self):
        self.cycle = BudgetCycle.objects.create(name="本版", budget_year=2027)
        self.project = Project.objects.create(code="A", name="本项目")
        self.other = Project.objects.create(code="B", name="其他项目")
        self.admin = User.objects.create_user(username="audit-admin", role="ADMIN")
        self.user = User.objects.create_user(username="audit-project", role="PROJECT", project=self.project)
        for project in (self.project, self.other):
            UploadVersion.objects.create(project=project, cycle=self.cycle, status="SUBMITTED")
        self.audit = {"summary": {"issue_count": 1, "expected_cells": 2, "read_cells": 1, "missing_cells": 1}, "issues": [
            {"report_code": "PL_TOTAL_WINE", "row_code": "R0032", "label": "酒店总收入", "period": "YEAR",
             "source_sheet": "汇总表", "source_cell": "J32", "reason_code": "CELL_EMPTY", "reason": "源单元格为空", "message": "汇总表J32为空"}]}

    @patch("budgeting.data_audit_views.build_upload_audit")
    def test_admin_can_filter_project_and_see_exact_cause(self, audit):
        audit.return_value = self.audit
        self.client.force_login(self.admin)
        response = self.client.get(reverse("data_audit"), {"cycle": self.cycle.pk, "project_id": self.project.pk, "reason": "CELL_EMPTY"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "酒店总收入")
        self.assertContains(response, "J32")
        self.assertContains(response, "源单元格为空")
        self.assertEqual(response.context["issue_page"].paginator.count, 1)

    @patch("budgeting.data_audit_views.build_upload_audit")
    def test_project_cannot_access_other_project_audit(self, audit):
        audit.return_value = self.audit
        self.client.force_login(self.user)
        response = self.client.get(reverse("data_audit"), {"cycle": self.cycle.pk})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "其他项目")
        response = self.client.get(reverse("data_audit"), {"cycle": self.cycle.pk, "project_id": self.other.pk})
        self.assertEqual(response.status_code, 404)

    def test_anonymous_requires_login(self):
        self.assertEqual(self.client.get(reverse("data_audit")).status_code, 302)
