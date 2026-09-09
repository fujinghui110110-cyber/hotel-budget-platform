import tempfile
import unittest
import zipfile
from pathlib import Path

from budgeting.services.workbook_reference import (
    build_reference_cache,
    load_index,
    load_sheet,
    read_workbook,
)
from budgeting.services.workbook_reference import _format_numeric


def _package(path: Path, *, macro: bool = False) -> None:
    workbook = """<?xml version="1.0" encoding="UTF-8"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets>
        <sheet name="Visible" sheetId="1" r:id="rId1"/>
        <sheet name="Hidden" sheetId="2" state="hidden" r:id="rId2"/>
      </sheets>
      <calcPr calcMode="auto"/>
    </workbook>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>
      <Relationship Id="rId2" Type="worksheet" Target="worksheets/sheet2.xml"/>
    </Relationships>"""
    sheet = """<?xml version="1.0" encoding="UTF-8"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <dimension ref="A1:XFD1048576"/>
      <sheetData>
        <row r="1">
          <c r="A1" t="inlineStr"><is><t>酒店</t></is></c>
          <c r="B1" s="1"><v>12.5</v></c>
          <c r="C1"><f>B1*2</f></c>
          <c r="XFD1" s="1"/>
          <c r="D1" t="e"><v>#REF!</v></c>
        </row>
      </sheetData>
    </worksheet>"""
    hidden = """<?xml version="1.0" encoding="UTF-8"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData><row r="3"><c r="A3"><v>7</v></c></row></sheetData>
    </worksheet>"""
    styles = """<?xml version="1.0" encoding="UTF-8"?>
    <styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="4"/></cellXfs>
    </styleSheet>"""
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
    <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
      <Default Extension="xml" ContentType="application/xml"/>
      <Override PartName="/xl/workbook.xml"
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
    </Types>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("xl/workbook.xml", workbook)
        package.writestr("xl/_rels/workbook.xml.rels", rels)
        package.writestr("xl/worksheets/sheet1.xml", sheet)
        package.writestr("xl/worksheets/sheet2.xml", hidden)
        package.writestr("xl/styles.xml", styles)
        if macro:
            package.writestr("xl/vbaProject.bin", b"not executed")
            package.writestr("xl/externalLinks/externalLink1.xml", b"not followed")


class WorkbookReferenceTests(unittest.TestCase):
    def test_number_formats_keep_finance_display_without_excel_tokens(self):
        accounting = '_ * #,##0.00_ ;_ * \\\\-#,##0.00_ ;_ * "-"??_ ;_ @_ '
        self.assertEqual(_format_numeric(3982000, "3982000", accounting), "3,982,000.00")
        self.assertEqual(
            _format_numeric(0.659959362915383, "0.659959362915383", "0.00%"),
            "66.00%",
        )
        self.assertEqual(_format_numeric(1.2, "1.2", "[Red]0.00"), "1.20")
        self.assertEqual(_format_numeric(1, "1", "m/d/yy"), "1900-01-01")

    def test_accounting_negative_sign_and_zero_literal_are_not_duplicated(self):
        self.assertEqual(_format_numeric(-12.5, "-12.5", '0.00;-0.00;"-"??'), "-12.50")
        self.assertEqual(_format_numeric(-12.5, "-12.5", '0.00;(0.00);"-"??'), "(12.50)")
        self.assertEqual(_format_numeric(-12.5, "-12.5", "0.00"), "-12.50")
        self.assertEqual(_format_numeric(0, "0", '0.00;-0.00;"-"??'), "-")

    def test_reader_keeps_formula_cache_gaps_errors_and_hidden_sheets(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hotel.xlsm"
            _package(source, macro=True)
            result = read_workbook(source)
            self.assertEqual(result["sheet_count"], 2)
            self.assertFalse(result["approved"])
            self.assertFalse(result["safety"]["vba_executed"])
            visible = result["sheets"][0]
            self.assertEqual(visible["state"], "visible")
            self.assertEqual(visible["col_count"], 4)
            self.assertEqual(visible["cell_count"], 4)
            cells = {cell["coordinate"]: cell for cell in visible["cells"]}
            self.assertEqual(cells["B1"]["display_value"], "12.50")
            self.assertIsNone(cells["C1"]["cached_value"])
            self.assertEqual(cells["C1"]["cache_status"], "missing")
            self.assertTrue(cells["C1"]["formula"].startswith("="))
            self.assertEqual(cells["D1"]["error_status"], "#REF!")
            self.assertEqual(result["sheets"][1]["state"], "hidden")

    def test_cache_index_and_bounded_row_paging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "sources"
            cache = root / "reference-workbooks"
            sources.mkdir()
            _package(sources / "3、万宁喜来登-2026年预算套表.xlsx")
            index = build_reference_cache(sources, cache)
            self.assertEqual(index["workbooks"][0]["id"], "wnxl")
            self.assertEqual(index["workbooks"][0]["sheet_count"], 2)
            self.assertFalse(index["approved"])
            self.assertEqual(load_index(cache)["stats"]["workbook_count"], 1)
            page = load_sheet("wnxl", "sheet-001", reference_dir=cache, start_row=1, max_rows=1)
            self.assertEqual(page["pagination"]["returned_cell_count"], 4)
            self.assertFalse(page["pagination"]["has_more_rows"])
            with self.assertRaises(ValueError):
                load_sheet("wnxl", "sheet-001", reference_dir=cache, max_rows=1, end_row=3)
            self.assertTrue((cache / "index.json").exists())
            self.assertTrue((cache / "wnxl" / "sheet-001.json").exists())


if __name__ == "__main__":
    unittest.main()
