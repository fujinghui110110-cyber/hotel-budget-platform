import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from budgeting.excel.ooxml import validate_upload_contract, validate_xlsx_zip


CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/xl/workbook.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
</Types>"""

WORKBOOK = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="SYS_META" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""

SHEET = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1"><v>1</v></c></row></sheetData>
</worksheet>"""


def _xlsx(path: Path, extra: dict[str, str | bytes] | None = None) -> None:
    def writestr(package, name, payload):
        if "\\" in name:
            info = zipfile.ZipInfo(name)
            info.filename = name
            package.writestr(info, payload)
        else:
            package.writestr(name, payload)

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        writestr(package, "[Content_Types].xml", CONTENT_TYPES)
        writestr(package, "xl/workbook.xml", WORKBOOK)
        writestr(package, "xl/_rels/workbook.xml.rels", RELS)
        writestr(package, "xl/worksheets/sheet1.xml", SHEET)
        for name, payload in (extra or {}).items():
            writestr(package, name, payload)


def _codes(path: Path) -> set[str]:
    return {issue[1] for issue in validate_xlsx_zip(path)}


class ZipPreflightShortCircuitTests(unittest.TestCase):
    def test_normal_directory_records_are_allowed_but_unsafe_directories_are_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "directories.xlsx"
            _xlsx(path, {"xl/": "", "xl/worksheets/": "", "_rels/": ""})
            self.assertEqual(validate_xlsx_zip(path), [])
            for name in ("../", "xl/../", "/xl/", "C:/xl/", "xl//", "xl//worksheets/", "xl/./", "xl\\worksheets/"):
                with self.subTest(name=name):
                    _xlsx(path, {name: ""})
                    self.assertIn(name.encode(), path.read_bytes())
                    self.assertIn("ZIP_TRAVERSAL", _codes(path))
            _xlsx(path, {"xl/": "", "xl": ""})
            self.assertIn("ZIP_DUPLICATE_ENTRY", _codes(path))

    def test_real_v3_templates_pass_preflight_with_large_xml_sheets(self):
        root = Path(__file__).resolve().parents[1]
        paths = [
            root / "artifacts/v3/2027/平台标准预算模板_V3_2027.xlsx",
            root / "artifacts/v3/2028/平台标准预算模板_V3_2028.xlsx",
        ]
        missing = [path for path in paths if not path.exists()]
        if missing:
            self.skipTest(f"真实V3模板样本不存在：{missing}")
        for path in paths:
            p0_issues = [issue for issue in validate_xlsx_zip(path) if issue[0] == "P0"]
            self.assertEqual(p0_issues, [], path)

    def test_metadata_p0_short_circuits_before_xml_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blocked.xlsx"
            dtd_sheet = """<?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE worksheet [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData><row r="1"><c r="A1"><v>&secret;</v></c></row></sheetData>
            </worksheet>"""
            with zipfile.ZipFile(
                path, "w", compression=zipfile.ZIP_DEFLATED
            ) as package:
                package.writestr("[Content_Types].xml", CONTENT_TYPES)
                package.writestr("xl/workbook.xml", WORKBOOK)
                package.writestr("xl/_rels/workbook.xml.rels", RELS)
                package.writestr("xl/worksheets/sheet1.xml", dtd_sheet)
                package.writestr("../evil.txt", "x")
                package.writestr("xl/vbaProject.bin", b"macro")
                package.writestr("xl/externalLinks/externalLink1.xml", "")
            with patch(
                "zipfile.ZipFile.open",
                side_effect=AssertionError(
                    "XML should not be opened after metadata P0"
                ),
            ):
                codes = _codes(path)
            self.assertIn("ZIP_TRAVERSAL", codes)
            self.assertIn("MACRO_OR_OLE", codes)
            self.assertIn("BLOCKED_PART", codes)
            self.assertNotIn("XML_ENTITY", codes)

    def test_preflight_blocks_utf16_dtd_and_external_relationships_after_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "xml-scan.xlsx"
            rels = RELS.replace(
                "</Relationships>",
                """
                <Relationship Id="rHyper" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
                  TargetMode = "External" Target="https://example.invalid/x"/>
                <Relationship Id="rData" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink"
                  TargetMode = "External" Target="externalLinks/externalLink1.xml"/>
                </Relationships>""",
            )
            dtd_sheet = (
                '<?xml version="1.0" encoding="UTF-16"?>\n'
                '<!DOCTYPE worksheet [<!ENTITY secret "x">]>\n'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<sheetData/></worksheet>"
            ).encode("utf-16")
            with zipfile.ZipFile(
                path, "w", compression=zipfile.ZIP_DEFLATED
            ) as package:
                package.writestr("[Content_Types].xml", CONTENT_TYPES)
                package.writestr("xl/workbook.xml", WORKBOOK)
                package.writestr("xl/_rels/workbook.xml.rels", rels)
                package.writestr("xl/worksheets/sheet1.xml", dtd_sheet)
            codes = _codes(path)
            self.assertIn("XML_ENTITY", codes)
            self.assertIn("EXTERNAL_HYPERLINK", codes)
            self.assertIn("EXTERNAL_RELATIONSHIP", codes)

    def test_preflight_blocks_raw_path_components_drives_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ambiguous.xlsx"
            with zipfile.ZipFile(
                path, "w", compression=zipfile.ZIP_DEFLATED
            ) as package:
                package.writestr("[Content_Types].xml", CONTENT_TYPES)
                package.writestr("xl/workbook.xml", WORKBOOK)
                package.writestr("xl/_rels/workbook.xml.rels", RELS)
                package.writestr("xl/worksheets/sheet1.xml", SHEET)
                package.writestr("xl/../worksheets/sheet2.xml", SHEET)
                package.writestr("C:/xl/worksheets/sheet3.xml", SHEET)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    package.writestr("xl/worksheets/sheet1.xml", SHEET)
            codes = _codes(path)
            self.assertIn("ZIP_TRAVERSAL", codes)
            self.assertIn("ZIP_DUPLICATE_ENTRY", codes)

    def test_preflight_limits_entry_count_unzipped_total_and_single_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "limits.xlsx"
            _xlsx(path, {"xl/worksheets/sheet2.xml": "x" * 40})
            with (
                patch("budgeting.excel.ooxml.MAX_ZIP_ENTRIES", 3),
                patch("budgeting.excel.ooxml.MAX_UNZIPPED_BYTES", 40),
                patch("budgeting.excel.ooxml.MAX_XML_BYTES", 10),
            ):
                codes = _codes(path)
            self.assertIn("ZIP_ENTRY_COUNT", codes)
            self.assertIn("UNZIPPED_SIZE", codes)
            self.assertIn("XML_SIZE", codes)

    def test_contract_validation_returns_preflight_before_reading_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short-circuit.xlsx"
            _xlsx(path, {"../evil.txt": "x"})
            upload = SimpleNamespace(template=None)
            with patch(
                "budgeting.excel.ooxml.read_sys_meta",
                side_effect=AssertionError("SYS_META should not be parsed"),
            ):
                codes = {issue[1] for issue in validate_upload_contract(upload, path)}
            self.assertIn("ZIP_TRAVERSAL", codes)
            self.assertNotIn("SYS_META", codes)


if __name__ == "__main__":
    unittest.main()
