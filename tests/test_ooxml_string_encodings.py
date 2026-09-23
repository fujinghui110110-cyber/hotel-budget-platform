import tempfile
import unittest
import zipfile
from pathlib import Path

from budgeting.excel.ooxml import formula_manifest, read_sys_meta


def _package(path: Path, *, sheet_xml: str, shared_strings: str | None = None) -> None:
    workbook = """<?xml version="1.0" encoding="UTF-8"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="SYS_META" sheetId="1" r:id="rId1"/></sheets>
    </workbook>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>
    </Relationships>"""
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
        package.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        if shared_strings is not None:
            package.writestr("xl/sharedStrings.xml", shared_strings)


class OOXMLStringEncodingTests(unittest.TestCase):
    def test_sys_meta_reads_shared_inline_and_rich_text(self):
        sheet = """<?xml version="1.0" encoding="UTF-8"?>
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <sheetData>
            <row r="1">
              <c r="A1" t="s"><v>0</v></c>
              <c r="B1" t="inlineStr"><is><r><t>V</t></r><r><t>3</t></r></is></c>
            </row>
            <row r="2">
              <c r="A2" t="inlineStr"><is><r><t>project_</t></r><r><t>code</t></r></is></c>
              <c r="B2" t="s"><v>1</v></c>
            </row>
            <row r="3">
              <c r="A3" t="inlineStr"><is><t>budget_year</t></is></c>
              <c r="B3"><v>2027</v></c>
            </row>
          </sheetData>
        </worksheet>"""
        shared = """<?xml version="1.0" encoding="UTF-8"?>
        <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <si><r><t>template_</t></r><r><t>version</t></r></si>
          <si><t>P001</t></si>
        </sst>"""
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "strings.xlsx"
            _package(workbook, sheet_xml=sheet, shared_strings=shared)
            self.assertEqual(
                read_sys_meta(workbook),
                {
                    "template_version": "V3",
                    "project_code": "P001",
                    "budget_year": "2027",
                },
            )

    def test_shared_formula_manifest_matches_equivalent_explicit_formula(self):
        explicit_sheet = """<?xml version="1.0" encoding="UTF-8"?>
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <sheetData>
            <row r="1"><c r="C1"><f>A1+B1</f></c></row>
            <row r="2"><c r="C2"><f>A2+B2</f></c></row>
          </sheetData>
        </worksheet>"""
        shared_sheet = """<?xml version="1.0" encoding="UTF-8"?>
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <sheetData>
            <row r="1"><c r="C1"><f t="shared" si="0" ref="C1:C2">A1+B1</f></c></row>
            <row r="2"><c r="C2"><f t="shared" si="0"/></c></row>
          </sheetData>
        </worksheet>"""
        with tempfile.TemporaryDirectory() as directory:
            explicit = Path(directory) / "explicit.xlsx"
            shared = Path(directory) / "shared.xlsx"
            _package(explicit, sheet_xml=explicit_sheet)
            _package(shared, sheet_xml=shared_sheet)
            explicit_rows, _ = formula_manifest(explicit)
            shared_rows, _ = formula_manifest(shared)
            self.assertEqual(shared_rows, explicit_rows)

    def test_shared_formula_translation_preserves_strings_and_absolute_refs(self):
        explicit_sheet = """<?xml version="1.0" encoding="UTF-8"?>
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <sheetData>
            <row r="1"><c r="C1"><f>"A1"&amp;A1+'Foo Bar'!A1+A$1+$B1+$C$1</f></c></row>
            <row r="2"><c r="C2"><f>"A1"&amp;A2+'Foo Bar'!A2+A$1+$B2+$C$1</f></c></row>
          </sheetData>
        </worksheet>"""
        shared_sheet = """<?xml version="1.0" encoding="UTF-8"?>
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <sheetData>
            <row r="1"><c r="C1"><f t="shared" si="0" ref="C1:C2">"A1"&amp;A1+'Foo Bar'!A1+A$1+$B1+$C$1</f></c></row>
            <row r="2"><c r="C2"><f t="shared" si="0"/></c></row>
          </sheetData>
        </worksheet>"""
        with tempfile.TemporaryDirectory() as directory:
            explicit = Path(directory) / "explicit.xlsx"
            shared = Path(directory) / "shared.xlsx"
            _package(explicit, sheet_xml=explicit_sheet)
            _package(shared, sheet_xml=shared_sheet)
            explicit_rows, _ = formula_manifest(explicit)
            shared_rows, _ = formula_manifest(shared)
            self.assertEqual(shared_rows, explicit_rows)


if __name__ == "__main__":
    unittest.main()
