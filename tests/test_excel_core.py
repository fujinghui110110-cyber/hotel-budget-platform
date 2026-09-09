import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from budgeting.excel.money import largest_remainder, yuan_to_cents
from budgeting.excel.ooxml import formula_manifest, validate_xlsx_zip


class ExcelCoreTests(unittest.TestCase):
    def test_money_round_half_up(self):
        self.assertEqual(yuan_to_cents("1.005"), 101)
        self.assertEqual(yuan_to_cents("-1.005"), -101)

    def test_largest_remainder_positive_and_negative(self):
        weights = {"P003": 0, "P001": 0, "P002": 0}
        self.assertEqual(
            largest_remainder(10000, weights),
            {"P001": 3334, "P002": 3333, "P003": 3333},
        )
        self.assertEqual(
            largest_remainder(-10000, weights),
            {"P001": -3334, "P002": -3333, "P003": -3333},
        )

    def test_zip_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.xlsx"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("[Content_Types].xml", "")
                zf.writestr("xl/workbook.xml", "")
                zf.writestr("../evil.txt", "x")
            codes = {issue[1] for issue in validate_xlsx_zip(path)}
            self.assertIn("ZIP_TRAVERSAL", codes)

    def test_formula_manifest_hash_is_stable(self):
        sample = Path(__file__).resolve().parents[1] / "tests" / "sample.xlsx"
        if not sample.exists():
            self.skipTest("sample workbook absent")
        _, a = formula_manifest(sample)
        _, b = formula_manifest(sample)
        self.assertEqual(a, b)

    def test_release_manifest_matches_runtime_formula_fingerprint(self):
        root = Path(__file__).resolve().parents[1]
        workbook = root / "artifacts" / "平台标准预算模板_V1.xlsx"
        manifest_path = root / "artifacts" / "template_manifest.json"
        if not workbook.exists() or not manifest_path.exists():
            self.skipTest("release artifact is not present")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows, digest = formula_manifest(workbook)
        self.assertEqual(len(rows), manifest["formula_count"])
        self.assertEqual(digest, manifest["formula_manifest_hash"])


if __name__ == "__main__":
    unittest.main()
