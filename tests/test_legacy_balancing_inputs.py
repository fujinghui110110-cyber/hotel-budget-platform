import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase
from openpyxl import Workbook

from budgeting.excel.legacy_adjustments import validate_legacy_balancing_inputs


class LegacyBalancingInputTests(SimpleTestCase):
    def test_nonzero_and_formula_balancing_inputs_are_blocked_without_editing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "旧模板.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "经营补充指标"
            sheet["A8"] = "送餐服务费尾差调整（元）"
            sheet["D8"] = 0.01
            sheet["E8"] = -0.01
            sheet["F8"] = "=0.01"
            sheet["G8"] = 0
            sheet["H8"] = "无依据"
            book.save(path)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch("budgeting.excel.legacy_adjustments.ValidationIssue.objects.get_or_create") as issue:
                validate_legacy_balancing_inputs(path, object())
            self.assertEqual(issue.call_count, 4)
            locations = {call.kwargs["location"] for call in issue.call_args_list}
            self.assertEqual(locations, {f"经营补充指标!{column}8" for column in "DEFH"})
            self.assertTrue(all(call.kwargs["defaults"]["severity"] == "P0" for call in issue.call_args_list))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_unrelated_legitimate_adjustments_are_not_deleted_or_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "旧模板.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "经营补充指标"
            sheet["A8"] = "送餐服务费尾差调整（元）"
            sheet["D5"] = 123.45
            sheet["D8"] = 0
            book.save(path)
            with patch("budgeting.excel.legacy_adjustments.ValidationIssue.objects.get_or_create") as issue:
                validate_legacy_balancing_inputs(path, object())
            issue.assert_not_called()
