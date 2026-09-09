from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from budgeting.models import Project, UploadVersion
from budgeting.templatetags.budget_tags import report_value


class ReferenceViewTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("source-admin", role="ADMIN")
        project = Project.objects.create(code="PRIVATE", name="其他项目")
        self.project_user = get_user_model().objects.create_user("source-project", role="PROJECT", project=project)
        self.book = {"id": "SZKL", "name": "深圳凯骊酒店", "sheet_count": 68,
                     "source_name": "深圳凯骊.xlsm", "sha256": "a" * 64,
                     "sheets": [{"id": "sheet-001", "name": "客房收入", "row_count": 1, "col_count": 3}]}

    @patch("budgeting.reference_views.list_workbooks")
    @patch("budgeting.reference_views.load_sheet")
    def test_original_values_are_visible_without_creating_approved_upload(self, loader, books):
        books.return_value = [self.book]
        loader.return_value = {"sheet": {"name": "客房收入", "row_count": 1, "col_count": 3, "error_count": 1},
                              "cells": [{"row": 1, "column": 1, "coordinate": "A1", "display_value": "0.00", "cached_value_raw": "0"},
                                        {"row": 1, "column": 2, "coordinate": "B1", "display_value": "#REF!", "cached_value_raw": "#REF!", "is_error": True, "formula": "=[1]外部!A1", "error_status": "#REF!"}]}
        self.client.force_login(self.admin)
        count = UploadVersion.objects.count()
        self.assertContains(self.client.get(reverse("reference_catalog")), "深圳凯骊酒店")
        response = self.client.get(reverse("reference_sheet", args=["SZKL", "sheet-001"]))
        for text in ("0.00", "#REF!", "=[1]外部!A1", "不是重新计算或已批准预算"):
            self.assertContains(response, text)
        self.assertTrue(response.context["rows"][0]["cells"][2]["blank"])
        self.assertEqual(UploadVersion.objects.count(), count)
        self.assertEqual(self.client.get(reverse("reference_sheet", args=["unknown", "sheet-001"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("reference_sheet", args=["SZKL", "sheet-001"]), {"row": "../"}).status_code, 404)

    def test_sources_are_not_accessible_to_other_project_accounts(self):
        url = reverse("reference_catalog")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.project_user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(reverse("reference_sheet", args=["SZKL", "sheet-001"])).status_code, 404)

    def test_missing_report_value_is_not_zero(self):
        self.assertEqual(report_value({}, "R1", "01"), "—")
        self.assertEqual(report_value({("R1", "01"): 0}, "R1", "01"), "0.00")

    @patch("budgeting.reference_views.list_workbooks")
    @patch("budgeting.reference_views.load_sheet")
    def test_business_header_default_keeps_full_original_accessible(self, loader, books):
        books.return_value = [self.book]
        cells = [{"row": 21, "column": 8, "cached_value": "项目", "display_value": "项目"}]
        cells += [{"row": 21, "column": month + 11, "cached_value": f"{month:02d}", "display_value": f"{month:02d}"} for month in range(1, 13)]
        loader.return_value = {"sheet": {"name": "客房收入", "row_count": 100, "col_count": 41}, "cells": cells}
        self.client.force_login(self.admin)
        url = reverse("reference_sheet", args=["SZKL", "sheet-001"])
        response = self.client.get(url)
        self.assertEqual((response.context["start_row"], response.context["start_col"]), (21, 8))
        full = self.client.get(url, {"row": 1, "col": 1})
        self.assertEqual((full.context["start_row"], full.context["start_col"]), (1, 1))
