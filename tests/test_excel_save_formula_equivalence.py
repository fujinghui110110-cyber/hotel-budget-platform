import unittest
from budgeting.excel.ooxml import formula_comparison_rows


class ExcelSaveFormulaEquivalenceTests(unittest.TestCase):
    def canonical(self, formula):
        return formula_comparison_rows([dict(sheet='S', cell='A1', formula=formula, shared_attributes={})], {'S'})

    def test_observed_excel_save_spellings(self):
        for before, after in [('01', '1'), ('B077', 'B77'), ("'经营补充指标'!D7+1", '经营补充指标!D7+1'), ('IFERROR(ROUND(#ref!,2),0)', 'IFERROR(ROUND(#REF!,2),0)')]:
            with self.subTest(before=before):
                self.assertEqual(self.canonical(before), self.canonical(after))

    def test_real_changes_and_literal_contents_remain_distinct(self):
        for before, after in [('01', '2'), ('B077', 'B78'), ('B077', '$B$77'), ('A1+B1', 'A1-B1'), ('"01"', '"1"'), ('INDIRECT("B077")', 'INDIRECT("B77")'), ("'经营补充指标'!D7", "'经营补充指标'!E7"), ('SUM(A1 B1)', 'SUM(A1,B1)')]:
            with self.subTest(before=before):
                self.assertNotEqual(self.canonical(before), self.canonical(after))

    def test_calculate_always_is_not_formula_or_array_extent(self):
        row = dict(sheet='S', cell='A1', formula='INDIRECT("B1")', shared_attributes={})
        expected = formula_comparison_rows([row], {'S'})
        self.assertEqual(expected, formula_comparison_rows([dict(row, shared_attributes={'ca': '1'})], {'S'}))
        self.assertNotEqual(expected, formula_comparison_rows([dict(row, shared_attributes={'t': 'array', 'ref': 'A1:A2'})], {'S'}))
        self.assertNotEqual(self.canonical('"#ref!"'), self.canonical('"#REF!"'))
