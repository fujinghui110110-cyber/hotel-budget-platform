from decimal import Decimal
from pathlib import Path
import json
import unittest

from budgeting.services.pnl_graph import MONTHS, ZZ_MONTH_COLS, _eval, build_report_graph, parse_formula


class TemplateManifestFormulaTests(unittest.TestCase):
    def test_2027_2028_zz_incentive_fee_uses_template_formula_without_loss_floor(self):
        manifest_paths = (
            Path("artifacts/v3/2027/template_manifest_V3_2027.json"),
            Path("artifacts/v3/2028/template_manifest_V3_2028.json"),
        )
        adjusted_gop = {month: Decimal(value) for month, value in zip(MONTHS, [-10000, 20000, 0, 15000, -5000, 30000, 12000, -8000, 45000, 1000, -12000, 6000])}
        month_by_col = dict(zip(ZZ_MONTH_COLS, MONTHS))

        for manifest_path in manifest_paths:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for report_code in ("PL_ZZ_WINE", "PL_ZZ_NOWINE"):
                rows, _num_to_code = build_report_graph(manifest, report_code)
                monthly_formula = rows["R0105"]["monthly"]
                annual_formula = rows["R0105"]["annual"]
                self.assertIn("0.04", monthly_formula, f"{manifest_path}:{report_code}")
                self.assertNotIn("MAX", monthly_formula.upper(), f"{manifest_path}:{report_code}")

                monthly_ast = parse_formula(monthly_formula)
                monthly_fee = {
                    month: _eval(
                        monthly_ast,
                        lambda _col, row, month=month: adjusted_gop[month] if row == 103 else Decimal(0),
                        lambda *_args: [],
                    )
                    for month in MONTHS
                }
                self.assertEqual(monthly_fee["01"], Decimal("-400.00"), f"{manifest_path}:{report_code}")

                annual_ast = parse_formula(annual_formula)
                annual_fee = _eval(
                    annual_ast,
                    lambda col, row: monthly_fee[month_by_col[col]] if row == 105 and col in month_by_col else Decimal(0),
                    lambda c1, r1, c2, r2: [monthly_fee[month] for month in MONTHS] if r1 == r2 == 105 else [],
                )
                self.assertEqual(annual_fee, sum(monthly_fee.values(), Decimal(0)), f"{manifest_path}:{report_code}")


if __name__ == "__main__":
    unittest.main()
