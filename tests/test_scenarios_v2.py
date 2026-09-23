import calendar
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.views import LoginView, LogoutView
from django.test import Client, TestCase, override_settings
from django.urls import include, path, reverse

from budgeting.excel.business_checks import TOTAL_RULES, ZZ_RULES
from budgeting.models import BudgetCycle, BudgetScenario, NormalizedValue, Project, ProjectCycle, TemplateVersion, UploadVersion
from budgeting.services.scenarios import (
    MONTHS,
    ScenarioError,
    calculate_fixed_cost_scenario,
    calculate_scenario,
    clone_scenario,
    issue_scenario,
)


urlpatterns = [
    path("login/", LoginView.as_view(template_name="budgeting/login.html"), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", include("budgeting.urls")),
    path("", include("budgeting.scenario_urls")),
]

REPORT_ROWS = {
    "PL_TOTAL_WINE": {
        "rooms": "R0023", "sellable": "R0024", "sold": "R0028", "occ": "R0029", "adr": "R0030",
        "room_rev": "R0041", "total": "R0032", "gop": "R0033", "operating": "R0090", "npi": "R0105", "final": "R0101",
    },
    "PL_TOTAL_NOWINE": {
        "rooms": "R0023", "sellable": "R0024", "sold": "R0028", "occ": "R0029", "adr": "R0030",
        "room_rev": "R0041", "total": "R0032", "gop": "R0033", "operating": "R0090", "npi": "R0105", "final": "R0101",
    },
    "PL_ZZ_WINE": {
        "rooms": "R0009", "sellable": "R0010", "sold": "R0011", "occ": "R0012", "adr": "R0013",
        "room_rev": "R0021", "total": "R0015", "gop": "R0016", "operating": "R0113", "npi": "R0129", "final": "R0127",
    },
    "PL_ZZ_NOWINE": {
        "rooms": "R0009", "sellable": "R0010", "sold": "R0011", "occ": "R0012", "adr": "R0013",
        "room_rev": "R0021", "total": "R0015", "gop": "R0016", "operating": "R0113", "npi": "R0129", "final": "R0127",
    },
}


def _full_report_baseline(report_code):
    is_zz = report_code.startswith("PL_ZZ")
    rules = ZZ_RULES if is_zz else TOTAL_RULES
    metric_rows = {
        "rooms": "R0009" if is_zz else "R0023",
        "sellable": "R0010" if is_zz else "R0024",
        "sold": "R0011" if is_zz else "R0028",
        "occ": "R0012" if is_zz else "R0029",
        "adr": "R0013" if is_zz else "R0030",
        "revpar": "R0014" if is_zz else "R0031",
        "room_rev": "R0021" if is_zz else "R0041",
        "gop": "R0016" if is_zz else "R0033",
    }
    numbers = set(rules)
    numbers.update(row for dependencies in rules.values() for row, _ in dependencies)
    numbers.update(int(code[1:]) for code in metric_rows.values())

    target_numbers = set(rules)
    values = {number: 1_000 for number in numbers if number not in target_numbers}
    values.update({
        int(metric_rows["rooms"][1:]): 100,
        int(metric_rows["sellable"][1:]): 1_000,
        int(metric_rows["sold"][1:]): 700,
        int(metric_rows["adr"][1:]): 100,
        int(metric_rows["revpar"][1:]): 70,
        int(metric_rows["room_rev"][1:]): 70_000,
        int(metric_rows["occ"][1:]): 0,
    })
    pending = dict(rules)
    while pending:
        progressed = False
        for target, dependencies in tuple(pending.items()):
            if any(row not in values for row, _ in dependencies):
                continue
            values[target] = sum(values[row] * sign for row, sign in dependencies)
            del pending[target]
            progressed = True
        if not progressed:
            raise AssertionError(f"profit rules are not topologically solvable: {sorted(pending)}")

    gop_target = 64 if is_zz else 80
    values[int(metric_rows["gop"][1:])] = values[gop_target]

    count_codes = {metric_rows["rooms"], metric_rows["sellable"], metric_rows["sold"]}
    ratio_codes = {metric_rows["occ"]}
    rows = {}
    for number in sorted(numbers):
        row_code = f"R{number:04d}"
        unit = "COUNT" if row_code in count_codes else "RATIO" if row_code in ratio_codes else "MONEY"
        if unit == "RATIO":
            cell = {"value_int": 0, "unit": unit, "ratio_num": 700, "ratio_den": 1_000}
            annual = {"value_int": 0, "unit": unit, "ratio_num": 8_400, "ratio_den": 12_000}
        else:
            value = int(values[number])
            cell = {"value_int": value, "unit": unit, "ratio_num": None, "ratio_den": None}
            annual_value = value if row_code in {metric_rows["rooms"], metric_rows["adr"], metric_rows["revpar"]} else value * 12
            annual = {"value_int": annual_value, "unit": unit, "ratio_num": None, "ratio_den": None}
        rows[row_code] = {
            "label": row_code,
            "unit": unit,
            "before": {month: dict(cell) for month in MONTHS} | {"YEAR": annual},
        }
    return rows


def _scenario_manifest_file(tempdir):
    reports = {}
    for report_code in REPORT_ROWS:
        zz = report_code.startswith("PL_ZZ")
        rules = ZZ_RULES if zz else TOTAL_RULES
        month_cols = "FGHIJKLMNOPQ" if zz else "LMNOPQRSTUVW"
        annual_col = "S" if zz else "J"
        month_col = month_cols[0]
        revpar_code = "R0014" if zz else "R0031"
        mapping = []
        for row_code, row in _full_report_baseline(report_code).items():
            number = int(row_code[1:])
            monthly_formula = ""
            if number in rules:
                parts = []
                for dependency, sign in rules[number]:
                    if not parts:
                        prefix = "" if sign > 0 else "-"
                    else:
                        prefix = "+" if sign > 0 else "-"
                    parts.append(f"{prefix}{month_col}{dependency}")
                monthly_formula = "".join(parts)
            if zz and row_code == "R0016":
                monthly_formula = f"{month_col}101"
            if zz and row_code == "R0105":
                monthly_formula = f"+{month_col}103*0.04"
            annual_formula = f"SUM({month_cols[0]}{number}:{month_cols[-1]}{number})"
            aggregation = "SUM"
            if row_code == REPORT_ROWS[report_code]["occ"]:
                sold = int(REPORT_ROWS[report_code]["sold"][1:])
                sellable = int(REPORT_ROWS[report_code]["sellable"][1:])
                monthly_formula = f"IF({month_col}{sellable}=0,0,{month_col}{sold}/{month_col}{sellable})"
                annual_formula = f"IF({annual_col}{sellable}=0,0,{annual_col}{sold}/{annual_col}{sellable})"
                aggregation = "RATIO"
            elif row_code == REPORT_ROWS[report_code]["adr"]:
                sold = int(REPORT_ROWS[report_code]["sold"][1:])
                room_rev = int(REPORT_ROWS[report_code]["room_rev"][1:])
                monthly_formula = f"ROUND(IF({month_col}{sold}=0,0,{month_col}{room_rev}/{month_col}{sold}),2)"
                annual_formula = f"ROUND(IF({annual_col}{sold}=0,0,{annual_col}{room_rev}/{annual_col}{sold}),2)"
                aggregation = "DERIVED"
            elif row_code == revpar_code:
                sellable = int(REPORT_ROWS[report_code]["sellable"][1:])
                room_rev = int(REPORT_ROWS[report_code]["room_rev"][1:])
                monthly_formula = f"ROUND(IF({month_col}{sellable}=0,0,{month_col}{room_rev}/{month_col}{sellable}),2)"
                annual_formula = f"ROUND(IF({annual_col}{sellable}=0,0,{annual_col}{room_rev}/{annual_col}{sellable}),2)"
                aggregation = "DERIVED"
            for cell, formula in ((f"{month_col}{number}", monthly_formula), (f"{annual_col}{number}", annual_formula)):
                mapping.append({
                    "row_code": row_code,
                    "row_label": row_code,
                    "cell": cell,
                    "source": {"cell": cell},
                    "formula": formula,
                    "unit": row["unit"],
                    "aggregation": aggregation,
                })
        reports[report_code] = {"mapping": mapping}
    path = Path(tempdir) / "scenario_manifest.json"
    path.write_text(json.dumps({"reports": reports}), encoding="utf-8")
    return str(path)


class ScenarioFixtureMixin:
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        manifest_path = _scenario_manifest_file(self.tempdir.name)
        User = get_user_model()
        self.admin = User.objects.create_user("scenario-admin", password="x", role="ADMIN", is_staff=True)
        self.project = Project.objects.create(code="P001", name="项目一")
        self.cycle = BudgetCycle.objects.create(name="2026", budget_year=2026, status=BudgetCycle.Status.ADJUSTING)
        self.template = TemplateVersion.objects.create(
            version="SCENARIO-V1", budget_year=2026, file_path="template.xlsx", manifest_path=manifest_path,
            formula_manifest_hash="0" * 64,
        )
        self.upload = UploadVersion.objects.create(
            project=self.project, cycle=self.cycle, template=self.template, status=UploadVersion.Status.APPROVED,
            original_name="budget.xlsx", original_path="uploads/p001/original.xlsx", sha256="a" * 64,
        )
        ProjectCycle.objects.create(project=self.project, cycle=self.cycle, current_upload=self.upload, is_open=True)
        self._seed_baseline()

    def _seed_baseline(self):
        for report_code, rows in REPORT_ROWS.items():
            monthly = {key: [] for key in rows}
            for month in MONTHS:
                days = calendar.monthrange(self.cycle.budget_year, int(month))[1]
                sellable = 200 * days
                sold = round(sellable * 0.70)
                room_rev = sold * 50_000
                total = room_rev * 13 // 10
                monthly.update({
                    "rooms": monthly["rooms"] + [(200, None, None)],
                    "sellable": monthly["sellable"] + [(sellable, None, None)],
                    "sold": monthly["sold"] + [(sold, None, None)],
                    "occ": monthly["occ"] + [(0, sold, sellable)],
                    "adr": monthly["adr"] + [(50_000, None, None)],
                    "room_rev": monthly["room_rev"] + [(room_rev, None, None)],
                    "total": monthly["total"] + [(total, None, None)],
                    "gop": monthly["gop"] + [(total * 3 // 10, None, None)],
                    "operating": monthly["operating"] + [(total * 24 // 100, None, None)],
                    "npi": monthly["npi"] + [(total * 20 // 100, None, None)],
                    "final": monthly["final"] + [(total * 18 // 100, None, None)],
                })
            totals = {
                "rooms": (200, None, None),
                "sellable": (sum(item[0] for item in monthly["sellable"]), None, None),
                "sold": (sum(item[0] for item in monthly["sold"]), None, None),
                "occ": (0, sum(item[1] for item in monthly["occ"]), sum(item[2] for item in monthly["occ"])),
                "adr": (50_000, None, None),
            }
            for key in ("room_rev", "total", "gop", "operating", "npi", "final"):
                totals[key] = (sum(item[0] for item in monthly[key]), None, None)
            for key, row_code in rows.items():
                unit = NormalizedValue.Unit.RATIO if key == "occ" else NormalizedValue.Unit.COUNT if key in {"rooms", "sellable", "sold"} else NormalizedValue.Unit.MONEY
                for month, (value, ratio_num, ratio_den) in zip(MONTHS, monthly[key]):
                    NormalizedValue.objects.create(
                        upload=self.upload, report_code=report_code, row_code=row_code, row_label=key,
                        period=month, unit=unit, value_int=value, ratio_num=ratio_num, ratio_den=ratio_den,
                        source_sheet=report_code, source_cell="C1",
                    )
                value, ratio_num, ratio_den = totals[key]
                NormalizedValue.objects.create(
                    upload=self.upload, report_code=report_code, row_code=row_code, row_label=key,
                    period="YEAR", unit=unit, value_int=value, ratio_num=ratio_num, ratio_den=ratio_den,
                    source_sheet=report_code, source_cell="C1",
                )


@override_settings(ROOT_URLCONF=__name__)
class ScenarioServiceTests(ScenarioFixtureMixin, TestCase):
    def test_partial_month_issue_counts_economic_delta_once_and_keeps_full_year(self):
        values = dict(NormalizedValue.objects.filter(
            upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0041", period__in=MONTHS,
        ).values_list("period", "value_int"))
        baseline_year = sum(values.values())
        values["01"] += 10_000
        scenario = clone_scenario(project=self.project, cycle=self.cycle, name="一月增加100元", created_by=self.admin)
        calculate_scenario(scenario, inputs={"driver": "ROOM_REV", "monthly": values}, actor=self.admin)
        batch = issue_scenario(scenario, actor=self.admin)
        self.assertEqual(batch.delta_cents, 10_000)
        self.assertEqual(batch.baseline_total_cents, baseline_year)
        self.assertEqual(batch.target_room_rev_cents, baseline_year + 10_000)
        self.assertEqual(batch.lines.count(), 4)
        self.assertEqual(sum(batch.lines.values_list("allocated_delta_cents", flat=True)), 40_000)
        self.assertEqual(issue_scenario(scenario, actor=self.admin).pk, batch.pk)
        current = ProjectCycle.objects.get(project=self.project, cycle=self.cycle)
        self.assertEqual(current.current_upload_id, self.upload.pk)
        project_user = get_user_model().objects.create_user("scenario-project", role="PROJECT", project=self.project)
        self.client.force_login(project_user)
        page = self.client.get(reverse("project_adjustments"))
        from budgeting.models import REPORTS
        for code in REPORT_ROWS:
            self.assertContains(page, REPORTS[code])
        self.assertContains(page, "100.00")
        from budgeting.services.workflow import issue_adjustment
        batch.status = "DRAFT"
        batch.save(update_fields=["status"])
        line = batch.lines.exclude(report_code="PL_TOTAL_WINE").first()
        line.allocated_delta_cents += 1
        line.target_cents += 1
        line.save(update_fields=["allocated_delta_cents", "target_cents"])
        with self.assertRaisesMessage(ValueError, "场景各报表差额"):
            issue_adjustment(batch, actor=self.admin)

    def test_calculate_four_reports_and_issue_idempotently(self):
        scenario = clone_scenario(project=self.project, cycle=self.cycle, name="出租率提升", created_by=self.admin)
        monthly = {month: "72" for month in MONTHS}
        calculate_scenario(scenario, inputs={"driver": "OCC", "monthly": monthly}, actor=self.admin)
        scenario.refresh_from_db()
        self.assertEqual(scenario.status, "READY")
        self.assertTrue(scenario.results["costs_fixed"])
        self.assertEqual(set(scenario.results["reports"]), set(REPORT_ROWS))
        self.assertTrue(all(scenario.results["reports"][code]["available"] for code in REPORT_ROWS))
        self.assertGreater(scenario.results["annual"]["delta_room_rev_cents"], 0)

        issue_scenario(scenario, actor=self.admin)
        scenario.refresh_from_db()
        self.assertEqual(scenario.status, "ISSUED")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("scenario_detail", args=[scenario.pk]))
        self.assertContains(response, "已下发，不可重新测算")
        self.assertNotContains(response, 'type="submit">保存并测算')
        cards = {card["code"]: card for card in response.context["report_cards"]}
        for report_code, metric_rows in REPORT_ROWS.items():
            labels = {row["code"]: row["unit_label"] for row in cards[report_code]["rows"]}
            self.assertEqual(labels[metric_rows["rooms"]], "间")
            self.assertEqual(labels[metric_rows["sold"]], "房晚")
            self.assertEqual(labels[metric_rows["sellable"]], "房晚")

class ScenarioPnlLinkageTests(TestCase):
    def test_four_reports_recalculate_rules_and_keep_cost_amount_rows(self):
        baseline = {report_code: _full_report_baseline(report_code) for report_code in REPORT_ROWS}
        result = calculate_fixed_cost_scenario(
            baseline,
            {"driver": "ROOM_REV", "monthly": {month: 80_000 for month in MONTHS}},
            budget_year=2026,
        )
        for report_code, report_rows in REPORT_ROWS.items():
            rows = {row["row_code"]: row for row in result["reports"][report_code]["rows"]}
            self.assertTrue(result["reports"][report_code]["available"])
            self.assertEqual(rows[report_rows["room_rev"]]["after"]["01"]["value_int"], 80_000)
            for key in ("total", "gop", "operating", "npi", "final"):
                row = rows[report_rows[key]]
                self.assertNotEqual(row["after"]["01"]["value_int"], row["before"]["01"]["value_int"])
                self.assertEqual(
                    row["after"]["YEAR"]["value_int"],
                    sum(row["after"][month]["value_int"] for month in MONTHS),
                )
            for row_code in ("R0050", "R0077"):
                row = rows[row_code]
                self.assertEqual(row["unit"], "MONEY")
                self.assertEqual(row["after"]["01"]["value_int"], row["before"]["01"]["value_int"])

    def test_missing_required_month_is_rejected(self):
        baseline = {"PL_TOTAL_WINE": _full_report_baseline("PL_TOTAL_WINE")}
        del baseline["PL_TOTAL_WINE"]["R0041"]["before"]["01"]
        with self.assertRaisesMessage(ScenarioError, "不能由年度数均摊"):
            calculate_fixed_cost_scenario(
                baseline,
                {"driver": "OCC", "monthly": {month: "72" for month in MONTHS}},
                budget_year=2026,
            )

    def test_annual_adr_and_revpar_use_weighted_year_denominators(self):
        baseline = {"PL_TOTAL_WINE": _full_report_baseline("PL_TOTAL_WINE")}
        result = calculate_fixed_cost_scenario(
            baseline,
            {"driver": "OCC", "monthly": {month: "72" for month in MONTHS}},
            budget_year=2026,
        )
        rows = {row["row_code"]: row for row in result["reports"]["PL_TOTAL_WINE"]["rows"]}
        adr = rows["R0030"]["after"]
        revpar = rows["R0031"]["after"]
        self.assertEqual(rows["R0030"]["before"]["YEAR"]["value_int"], 100)
        self.assertEqual(rows["R0031"]["before"]["YEAR"]["value_int"], 70)
        self.assertEqual(adr["YEAR"]["value_int"], 100)
        self.assertEqual(revpar["YEAR"]["value_int"], 72)
        self.assertNotEqual(adr["YEAR"]["value_int"], sum(adr[month]["value_int"] for month in MONTHS))
        self.assertNotEqual(revpar["YEAR"]["value_int"], sum(revpar[month]["value_int"] for month in MONTHS))

    def test_annual_room_revenue_delta_is_allocated_to_months_exactly(self):
        baseline = {"PL_TOTAL_WINE": _full_report_baseline("PL_TOTAL_WINE")}
        result = calculate_fixed_cost_scenario(
            baseline,
            {"driver": "ROOM_REV", "annual_room_rev_delta": "0.01"},
            budget_year=2026,
        )
        deltas = [item["delta_room_rev_cents"] for item in result["reports"]["PL_TOTAL_WINE"]["monthly"]]
        self.assertEqual(sum(deltas), 10_000)
        self.assertEqual(sorted(deltas), [833] * 8 + [834] * 4)


@override_settings(ROOT_URLCONF=__name__)
class ScenarioViewTests(ScenarioFixtureMixin, TestCase):
    def test_admin_can_list_and_create_scenario(self):
        client = Client()
        client.force_login(self.admin)
        response = client.get(reverse("scenario_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "固定成本敏感性场景")
        response = client.post(reverse("scenario_new"), {"cycle": self.cycle.pk, "project": self.project.pk, "name": "新场景"})
        self.assertEqual(response.status_code, 302)
        scenario = BudgetScenario.objects.get(name="新场景")
        self.assertEqual(response["Location"], reverse("scenario_detail", kwargs={"scenario_id": scenario.pk}))

    def test_project_user_cannot_access_scenarios(self):
        User = get_user_model()
        user = User.objects.create_user("p001", password="x", role="PROJECT", project=self.project)
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(reverse("scenario_list")).status_code, 404)

    def test_calculate_and_issue_posts(self):
        scenario = clone_scenario(project=self.project, cycle=self.cycle, name="表单场景", created_by=self.admin)
        client = Client()
        client.force_login(self.admin)
        data = {"driver": "OCC"}
        data.update({f"month_{month}": "72" for month in MONTHS})
        response = client.post(reverse("scenario_calculate", kwargs={"scenario_id": scenario.pk}), data)
        self.assertEqual(response.status_code, 302)
        scenario.refresh_from_db()
        self.assertEqual(scenario.status, "READY")
        response = client.post(reverse("scenario_issue", kwargs={"scenario_id": scenario.pk}))
        self.assertEqual(response.status_code, 302)
        scenario.refresh_from_db()
        self.assertEqual(scenario.status, "ISSUED")

    def test_detail_formats_metric_units_and_draft_state(self):
        scenario = clone_scenario(project=self.project, cycle=self.cycle, name="展示场景", created_by=self.admin)
        client = Client()
        client.force_login(self.admin)

        draft = client.get(reverse("scenario_detail", kwargs={"scenario_id": scenario.pk}))
        self.assertEqual(draft.status_code, 200)
        self.assertContains(draft, "尚未测算")
        self.assertNotContains(draft, "基线暂无数据")
        self.assertContains(draft, "年度客房收入目标（万元）")
        self.assertContains(draft, 'data-scenario-driver="room-rev"')
        self.assertContains(draft, "年度客房收入增量（万元，可选）")

        data = {"driver": "OCC"}
        data.update({f"month_{month}": "72" for month in MONTHS})
        response = client.post(reverse("scenario_calculate", kwargs={"scenario_id": scenario.pk}), data)
        self.assertEqual(response.status_code, 302)
        detail = client.get(response["Location"])
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "元/房晚")
        self.assertContains(detail, "500.00")
        self.assertContains(detail, "360.00")
        self.assertContains(detail, "72.00%")

    def test_detail_exposes_dynamic_planning_context_and_preserves_form_choices(self):
        self.cycle.budget_year = 2027
        self.cycle.save(update_fields=["budget_year"])
        scenario = clone_scenario(project=self.project, cycle=self.cycle, name="年度动态场景", created_by=self.admin)
        client = Client()
        client.force_login(self.admin)

        response = client.post(
            reverse("scenario_calculate", kwargs={"scenario_id": scenario.pk}),
            {"driver": "OCC", "cost_mode": "fixed", "uniform_value": "72"},
        )
        self.assertEqual(response.status_code, 302)
        detail = client.get(response["Location"])

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            [(item["year"], item["kind"]) for item in detail.context["scenario_history_years"]],
            [(2024, "ACTUAL"), (2025, "ACTUAL"), (2026, "FORECAST"), (2027, "BUDGET")],
        )
        self.assertEqual(len(detail.context["scenario_summary_rows"]), 6)
        self.assertEqual(len(detail.context["scenario_history_rows"]), 6)
        self.assertEqual(detail.context["form_data"]["cost_mode"], "fixed")
        self.assertEqual(detail.context["form_data"]["uniform_value"], "72")
        self.assertContains(detail, "2024 实际")
        self.assertContains(detail, "2027 原预算")
        self.assertContains(detail, "value=\"fixed\" selected")
        self.assertRegex(detail.content.decode(), r'name="uniform_value"[^>]*value="72"')
