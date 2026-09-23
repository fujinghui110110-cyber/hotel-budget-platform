from unittest import TestCase
from xml.etree import ElementTree as ET

from scripts.purify_template import M, NS, enforce_strict_annual_formulas


class StrictAnnualFormulaTests(TestCase):
    def test_materializes_every_annual_aggregation_contract(self):
        xml = (
            f'<worksheet xmlns="{M}"><sheetData>'
            '<row r="23"><c r="J23"><f t="shared" si="1"/><v>0</v></c>'
            '<c r="L23"><v>178</v></c></row>'
            '<row r="29"><c r="J29"><f/><v>0</v></c>'
            '<c r="L29"><v>0.5</v></c></row>'
            '<row r="34"><c r="J34"><f t="shared" si="12"/><v>0</v></c>'
            '<c r="L34"><v>1</v></c></row>'
            '<row r="37"><c r="J37"><f/><v>0</v></c>'
            '<c r="L37"><v>2</v></c></row>'
            '<row r="40"><c r="J40"><f/><v>0</v></c>'
            '<c r="L40"><v>3.14</v></c></row>'
            '</sheetData></worksheet>'
        )
        root = ET.fromstring(xml)
        report = {"annual_formulas_rewritten": []}

        enforce_strict_annual_formulas(
            root, "酒店损益总表（不含名酒）", report
        )

        expected = {
            "J23": "IF(COUNT(L23:W23)=0,0,ROUND(AVERAGE(L23:W23),0))",
            "J29": "IF(J24=0,0,J28/J24)",
            "J34": "SUM(L34:W34)",
            "J37": "ROUND(IF(J35=0,0,J49/J35),2)",
            "J40": (
                "ROUND(SUM(ROUND(L40,2),ROUND(M40,2),ROUND(N40,2),"
                "ROUND(O40,2),ROUND(P40,2),ROUND(Q40,2),ROUND(R40,2),"
                "ROUND(S40,2),ROUND(T40,2),ROUND(U40,2),ROUND(V40,2),"
                "ROUND(W40,2)),2)"
            ),
        }
        for cell_ref, formula in expected.items():
            cell = root.find(f".//m:c[@r='{cell_ref}']", NS)
            formula_node = cell.find("m:f", NS)
            self.assertEqual(formula_node.text, formula)
            self.assertEqual(formula_node.attrib, {})
            self.assertIsNone(cell.find("m:v", NS))


class HeaderLiteralTests(TestCase):
    def test_view_and_subnm_labels_are_excel_strings(self):
        from scripts.purify_template import replace_proprietary
        self.assertEqual(replace_proprietary('VIEW("server","cube","!")'), ('"!"', True, None))
        self.assertEqual(replace_proprietary('SUBNM("server","dimension","2020一上版")'), ('"2020一上版"', True, None))
        self.assertEqual(replace_proprietary('SUBNM("server","dimension","cny")'), ('"cny"', True, None))
