import unittest

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from budgeting.excel.extract import _sub_annual
from budgeting.models import (
    AdjustmentBatch,
    AdjustmentLine,
    BudgetCycle,
    NormalizedValue,
    Project,
    ProjectCycle,
    TemplateVersion,
    UploadVersion,
)
from budgeting.services.drivers import simulate_driver
from budgeting.services.workflow import (
    create_driver_adjustment,
    issue_adjustment,
    update_adjustment_lines_for_upload,
)

REPORT = "PL_TOTAL_WINE"


def _baseline():
    rooms, sellable = 200, 73000
    sold = round(sellable * 0.7)
    adr = 50_000
    room_rev = sold * adr
    revpar = round(room_rev / sellable)
    return {
        "R0023": {"value_int": rooms, "unit": "COUNT", "ratio_num": 0, "ratio_den": 0},
        "R0024": {"value_int": sellable, "unit": "COUNT", "ratio_num": 0, "ratio_den": 0},
        "R0028": {"value_int": sold, "unit": "COUNT", "ratio_num": 0, "ratio_den": 0},
        "R0029": {"value_int": 0, "unit": "RATIO", "ratio_num": 7000, "ratio_den": 10000},
        "R0030": {"value_int": adr, "unit": "MONEY", "ratio_num": 0, "ratio_den": 0},
        "R0031": {"value_int": revpar, "unit": "MONEY", "ratio_num": 0, "ratio_den": 0},
        "R0041": {"value_int": room_rev, "unit": "MONEY", "ratio_num": 0, "ratio_den": 0},
        "R0032": {"value_int": 3_000_000_000, "unit": "MONEY", "ratio_num": 0, "ratio_den": 0},
        "R0033": {"value_int": 800_000_000, "unit": "MONEY", "ratio_num": 0, "ratio_den": 0},
        "R0101": {"value_int": 500_000_000, "unit": "MONEY", "ratio_num": 0, "ratio_den": 0},
    }


def _proj(snapshot, key):
    return {row[0]: row[4] for row in snapshot["rows"]}[key]


class SimulateDriverTests(unittest.TestCase):
    def test_occ_cascades_1_to_1(self):
        snap = simulate_driver(_baseline(), "OCC", 72)
        self.assertEqual(_proj(snap, "SOLD"), 52_560)
        self.assertEqual(_proj(snap, "ROOM_REV"), 2_628_000_000)
        self.assertEqual(_proj(snap, "REVPAR"), 36_000)
        self.assertEqual(snap["delta_room_rev"], 73_000_000)
        self.assertEqual(_proj(snap, "TOTAL_REV"), 3_073_000_000)
        self.assertEqual(_proj(snap, "GOP"), 873_000_000)
        self.assertEqual(_proj(snap, "NET"), 573_000_000)

    def test_adr_cascades_1_to_1(self):
        snap = simulate_driver(_baseline(), "ADR", 520)
        self.assertEqual(_proj(snap, "SOLD"), 51_100)
        self.assertEqual(_proj(snap, "ROOM_REV"), 2_657_200_000)
        self.assertEqual(_proj(snap, "REVPAR"), 36_400)
        self.assertEqual(_proj(snap, "NET"), 602_200_000)

    def test_rooms_recomputes_sellable(self):
        snap = simulate_driver(_baseline(), "ROOMS", 220)
        self.assertEqual(_proj(snap, "SELLABLE"), 80_300)
        self.assertEqual(_proj(snap, "SOLD"), 56_210)
        self.assertEqual(_proj(snap, "ROOM_REV"), 2_810_500_000)
        self.assertEqual(_proj(snap, "REVPAR"), 35_000)

    def test_room_rev_implies_adr(self):
        snap = simulate_driver(_baseline(), "ROOM_REV", 3200)
        self.assertEqual(_proj(snap, "ROOM_REV"), 3_200_000_000)
        self.assertEqual(_proj(snap, "ADR"), 62_622)
        self.assertEqual(_proj(snap, "REVPAR"), 43_836)
        self.assertEqual(_proj(snap, "NET"), 1_145_000_000)

    def test_profit_linkage_is_delta(self):
        snap = simulate_driver(_baseline(), "ADR", 520)
        delta = snap["delta_room_rev"]
        self.assertEqual(_proj(snap, "TOTAL_REV") - 3_000_000_000, delta)
        self.assertEqual(_proj(snap, "GOP") - 800_000_000, delta)
        self.assertEqual(_proj(snap, "NET") - 500_000_000, delta)

    def test_zero_rooms_falls_back_to_365(self):
        baseline = _baseline()
        baseline["R0023"]["value_int"] = 0
        baseline["R0024"]["value_int"] = 0
        snap = simulate_driver(baseline, "ROOMS", 200)
        self.assertEqual(_proj(snap, "SELLABLE"), 73_000)

    def test_missing_row_raises(self):
        baseline = _baseline()
        del baseline["R0101"]
        with self.assertRaises(ValueError):
            simulate_driver(baseline, "OCC", 72)

    def test_occ_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            simulate_driver(_baseline(), "OCC", 150)


class SubAnnualTests(unittest.TestCase):
    def test_sums_money(self):
        monthly = [
            NormalizedValue(value_int=100, unit="MONEY", ratio_num=None, ratio_den=None),
            NormalizedValue(value_int=200, unit="MONEY", ratio_num=None, ratio_den=None),
        ]
        annual = _sub_annual(None, "SUB", "s", "R0001", "lbl", "MONEY", monthly)
        self.assertEqual(annual.period, "YEAR")
        self.assertEqual(annual.value_int, 300)

    def test_weights_ratio(self):
        monthly = [
            NormalizedValue(value_int=0, unit="RATIO", ratio_num=6000, ratio_den=10000),
            NormalizedValue(value_int=0, unit="RATIO", ratio_num=4000, ratio_den=5000),
        ]
        annual = _sub_annual(None, "SUB", "s", "R0001", "lbl", "RATIO", monthly)
        self.assertEqual(annual.ratio_num, 10000)
        self.assertEqual(annual.ratio_den, 15000)
        self.assertEqual(annual.value_int, 0)


class DriverAdjustmentFlowTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(code="P001", name="项目一")
        self.cycle = BudgetCycle.objects.create(
            name="2026", budget_year=2026, status=BudgetCycle.Status.OPEN
        )
        self.template = TemplateVersion.objects.create(
            version="V1", budget_year=2026, file_path="x.xlsx",
            manifest_path="m.json", formula_manifest_hash="0" * 64,
        )
        self.upload = UploadVersion.objects.create(
            project=self.project, cycle=self.cycle, template=self.template,
            status=UploadVersion.Status.APPROVED, original_path="o.xlsx", sha256="a" * 64,
        )
        ProjectCycle.objects.create(
            project=self.project, cycle=self.cycle, current_upload=self.upload, is_open=True
        )
        self._seed_year(self.upload)

    def _seed_year(self, upload):
        specs = [
            ("R0023", "COUNT", 200), ("R0024", "COUNT", 73_000), ("R0028", "COUNT", 51_100),
            ("R0030", "MONEY", 50_000), ("R0031", "MONEY", 35_000),
            ("R0041", "MONEY", 2_555_000_000), ("R0032", "MONEY", 3_000_000_000),
            ("R0033", "MONEY", 800_000_000), ("R0101", "MONEY", 500_000_000),
        ]
        for code, unit, value in specs:
            NormalizedValue.objects.create(
                upload=upload, report_code=REPORT, row_code=code, row_label=code,
                period="YEAR", unit=unit, value_int=value, source_sheet="s", source_cell="c",
            )
        NormalizedValue.objects.create(
            upload=upload, report_code=REPORT, row_code="R0029", row_label="出租率",
            period="YEAR", unit="RATIO", value_int=0, ratio_num=7000, ratio_den=10000,
            source_sheet="s", source_cell="c",
        )

    def test_create_issue_confirm_loop(self):
        batch = create_driver_adjustment(self.cycle, self.project, REPORT, "OCC", 72, "test")
        self.assertEqual(batch.driver, "OCC")
        self.assertEqual(batch.project, self.project)
        self.assertEqual(batch.target_room_rev_cents, 2_628_000_000)
        self.assertEqual(batch.lines.count(), 1)

        line = batch.lines.first()
        self.assertEqual(line.row_code, "R0041")
        self.assertEqual(line.target_cents, 2_628_000_000)
        self.assertEqual(line.status, AdjustmentLine.Status.OPEN)

        issue_adjustment(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.status, AdjustmentBatch.Status.ISSUED)

        new_upload = UploadVersion.objects.create(
            project=self.project, cycle=self.cycle, template=self.template,
            status=UploadVersion.Status.APPROVED, original_path="o2.xlsx", sha256="b" * 64,
        )
        NormalizedValue.objects.create(
            upload=new_upload, report_code=REPORT, row_code="R0041", row_label="客房收入",
            period="YEAR", unit="MONEY", value_int=2_628_000_000,
            source_sheet="s", source_cell="c",
        )
        update_adjustment_lines_for_upload(new_upload)
        line.refresh_from_db()
        self.assertEqual(line.status, AdjustmentLine.Status.CONFIRMED)
        batch.refresh_from_db()
        self.assertEqual(batch.status, AdjustmentBatch.Status.COMPLETED)


class DriverViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("admin", password="x", role="ADMIN", is_staff=True)
        self.project = Project.objects.create(code="P001", name="项目一")
        self.puser = User.objects.create_user("p001", password="x", role="PROJECT", project=self.project)
        BudgetCycle.objects.create(name="2026", budget_year=2026, status=BudgetCycle.Status.OPEN)

    def test_management_adjustments_200(self):
        client = Client()
        client.force_login(self.admin)
        response = client.get("/management/adjustments/", HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)

    def test_project_adjustments_200(self):
        client = Client()
        client.force_login(self.puser)
        response = client.get("/project/adjustments/", HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
