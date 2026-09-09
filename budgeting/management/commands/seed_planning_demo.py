import calendar
import hashlib
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from budgeting.excel.business_checks import TOTAL_RULES, ZZ_RULES
from budgeting.models import BudgetCycle, NormalizedValue, Project, ProjectCycle, UploadVersion, User
from budgeting.services.metrics import METRICS, rolling_years


DEMO_SUPPLEMENTARY_ROWS = {
    "R9001": "餐厅收入",
    "R9002": "宴会收入",
    "R9003": "名酒收入（分析补充）",
    "R9004": "客房运营成本合计（含人工）",
    "R9005": "季节性产品收入（月饼亭）",
}


def rounded(value):
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def month_values(report, year, month, project_index):
    split = report.startswith("PL_ZZ")
    rooms = 180 + project_index * 65
    available = rooms * calendar.monthrange(year, month)[1]
    occupancy = Decimal("0.66") + Decimal(project_index) / 50 + Decimal((month * 3) % 11) / 100
    sold = rounded(available * occupancy)
    adr = (460 + project_index * 55 + (year - 2024) * 18 + ((month * 7) % 9) * 8) * 100
    revenue = sold * adr
    food = rounded(revenue * Decimal("0.25"))
    drink = rounded(revenue * Decimal("0.04"))
    other_fb = rounded(revenue * (Decimal("0.06") if report.endswith("NOWINE") else Decimal("0.08")))
    other = rounded(revenue * Decimal("0.04"))
    rent = rounded(revenue * Decimal("0.03"))
    values = {row: 0 for row in range(1, 146)}
    if split:
        values.update({9: rooms, 10: available, 11: sold, 13: adr, 14: rounded(Decimal(revenue) / available),
                       21: revenue, 23: rounded(revenue * Decimal("0.10")), 24: rounded(revenue * Decimal("0.03")),
                       25: rounded(revenue * Decimal("0.02")), 26: rounded(revenue * Decimal("0.02")),
                       27: rounded(revenue * Decimal("0.06")), 30: food, 31: drink, 32: other_fb,
                       35: rounded(food * Decimal("0.30")), 36: rounded(drink * Decimal("0.20")),
                       37: rounded(other_fb * Decimal("0.18")), 40: rounded(revenue * Decimal("0.04")),
                       44: rounded(revenue * Decimal("0.02")), 48: other, 49: rounded(other * Decimal("0.20")),
                       51: rounded(other * Decimal("0.10")), 55: rounded(other * Decimal("0.10")),
                       59: rent, 60: rounded(rent * Decimal("0.05")), 61: rounded(rent * Decimal("0.02")),
                       72: rounded(revenue * Decimal("0.04")), 73: rounded(revenue * Decimal("0.015")),
                       84: rounded(revenue * Decimal("0.01")), 92: rounded(revenue * Decimal("0.01")),
                       93: rounded(revenue * Decimal("0.012")), 94: rounded(revenue * Decimal("0.018")),
                       96: rounded(revenue * Decimal("0.025")), 97: rounded(revenue * Decimal("0.045")),
                       102: rounded(revenue * Decimal("0.01")), 105: rounded(revenue * Decimal("0.02")),
                       115: rounded(revenue * Decimal("0.02")), 117: rounded(revenue * Decimal("0.06")),
                       120: rounded(revenue * Decimal("0.03"))})
        rules, gop_row, gop_source, profit_row, tax_row = ZZ_RULES, 16, 101, 124, 126
    else:
        values.update({23: rooms, 24: available, 25: available - sold, 26: rounded(available * Decimal("0.01")),
                       27: rounded(available * Decimal("0.005")), 28: sold, 30: adr,
                       31: rounded(Decimal(revenue) / available), 41: revenue,
                       42: rounded(revenue * Decimal("0.17")), 43: rounded(revenue * Decimal("0.06")),
                       45: food, 46: drink, 47: other_fb, 50: rounded(food * Decimal("0.30")),
                       51: rounded(drink * Decimal("0.20")), 52: rounded(other_fb * Decimal("0.18")),
                       54: rounded(revenue * Decimal("0.04")), 55: rounded(revenue * Decimal("0.02")),
                       57: other, 58: rounded(other * Decimal("0.20")), 59: rounded(other * Decimal("0.10")),
                       60: rounded(other * Decimal("0.10")), 62: rent, 63: rounded(rent * Decimal("0.05")),
                       64: rounded(rent * Decimal("0.02")), 67: rounded(revenue * Decimal("0.04")),
                       68: rounded(revenue * Decimal("0.015")), 72: rounded(revenue * Decimal("0.01")),
                       73: rounded(revenue * Decimal("0.01")), 74: rounded(revenue * Decimal("0.012")),
                       75: rounded(revenue * Decimal("0.018")), 76: rounded(revenue * Decimal("0.025")),
                       77: rounded(revenue * Decimal("0.045")), 81: rounded(revenue * Decimal("0.01")),
                       83: rounded(revenue * Decimal("0.02")), 91: rounded(revenue * Decimal("0.02")),
                       93: rounded(revenue * Decimal("0.06")), 96: rounded(revenue * Decimal("0.03"))})
        rules, gop_row, gop_source, profit_row, tax_row = TOTAL_RULES, 33, 80, 99, 100
    for _ in range(2):
        for target, terms in rules.items():
            values[target] = sum(values[row] * sign for row, sign in terms)
        values[tax_row] = rounded(max(0, values[profit_row]) * Decimal("0.25"))
    values[gop_row] = values[gop_source]
    return values


def supplementary_values(report, values):
    if report.startswith("PL_ZZ"):
        restaurant = values[30]
        banquet = values[32]
        wine = values[31]
        operating_cost = sum(values[row] for row in range(23, 28))
    else:
        restaurant = values[45]
        banquet = values[47]
        wine = values[46]
        operating_cost = values[42] + values[43]
    return {
        "R9001": restaurant,
        "R9002": banquet,
        "R9003": wine,
        "R9004": operating_cost,
        "R9005": rounded(Decimal(restaurant) * Decimal("0.01")),
    }


class Command(BaseCommand):
    help = "Populate an empty local database with clearly identified four-year demonstration budgets."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=date.today().year + 1)

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("正式环境禁止创建演示账号，请使用 createsuperuser 和组织账号管理。")
        if Project.objects.exists() or User.objects.exists():
            raise CommandError("只允许初始化空数据库；不会覆盖已有项目或账号。")
        year = options["year"]
        if year not in range(2003, 2101):
            raise CommandError("预算年度需在 2003 至 2100 之间。")
        manifest = json.loads((settings.BASE_DIR / "artifacts/template_manifest.json").read_text())
        cycle = BudgetCycle.objects.create(name=f"{year} 年预算（演示）", budget_year=year, status="OPEN")
        user = User.objects.create(username="admin", role="ADMIN", is_staff=True, is_superuser=True,
                                   first_name="预算管理者")
        user.set_password("admin123")
        user.save()
        for index, name in enumerate(("滨海国际酒店", "城市中心商务酒店", "山湖度假酒店")):
            project = Project.objects.create(code=f"DEMO{index + 1:02d}", name=f"{name}（演示）")
            account = User.objects.create(username=f"project{index + 1}", role="PROJECT", project=project)
            account.set_password("project123")
            account.save()
            upload = UploadVersion.objects.create(project=project, cycle=cycle, status="APPROVED",
                original_name=f"{name}-合成演示预算.xlsx", original_path="demo/no-source-workbook.xlsx",
                sha256=hashlib.sha256(f"demo:{year}:{index}".encode()).hexdigest())
            pending = []
            for report, report_meta in manifest["reports"].items():
                metadata = {item["row_code"]: item for item in report_meta["mapping"]}
                for data_year, kind in rolling_years(year):
                    monthly = [month_values(report, data_year, month, index) for month in range(1, 13)]
                    for row_code, meta in metadata.items():
                        number = int(row_code[1:])
                        unit = meta.get("unit", "MONEY")
                        aggregation = meta.get("aggregation", "SUM")
                        numerator = denominator = None
                        for spec in METRICS.values():
                            if (spec.get("rows") or {}).get(report) == row_code:
                                numerator = (spec.get("numerator_rows") or {}).get(report)
                                denominator = (spec.get("denominator_rows") or {}).get(report)
                                if numerator and denominator:
                                    break
                        for month in [*range(1, 13), None]:
                            selected = [monthly[month - 1]] if month else monthly
                            num = sum(values.get(int(numerator[1:]), 0) for values in selected) if numerator else None
                            den = sum(values.get(int(denominator[1:]), 0) for values in selected) if denominator else None
                            value = sum(values.get(number, 0) for values in selected)
                            if unit == "RATIO":
                                value = rounded(Decimal(num or 0) / den * 10000) if den else 0
                            elif aggregation == "DERIVED" and num is not None and den:
                                value = rounded(Decimal(num) / den)
                            elif aggregation == "AVERAGE" or number in ({13, 14, 18} if report.startswith("PL_ZZ") else {30, 31, 37, 38}):
                                value = rounded(Decimal(value) / len(selected))
                            if number in ({13, 14} if report.startswith("PL_ZZ") else {30, 31}):
                                rv = 21 if report.startswith("PL_ZZ") else 41
                                base = (11 if number == 13 else 10) if report.startswith("PL_ZZ") else (28 if number == 30 else 24)
                                den = sum(values[base] for values in selected)
                                num = sum(values[rv] for values in selected)
                                value = rounded(Decimal(num) / den) if den else 0
                            if kind == "BUDGET":
                                period = f"{month:02d}" if month else "YEAR"
                            else:
                                period = ("A" if kind == "ACTUAL" else "F") + str(data_year) + (f"M{month:02d}" if month else "")
                            pending.append(NormalizedValue(upload=upload, report_code=report, row_code=row_code,
                                row_label=meta.get("row_label", row_code), period=period, data_year=data_year,
                                data_kind=kind, month=month, unit=unit, value_int=value,
                                ratio_num=num if unit == "RATIO" else None, ratio_den=den if unit == "RATIO" else None,
                                source_sheet="合成演示数据（无真实项目来源）", source_cell="DEMO"))
                    supplementary_monthly = [
                        supplementary_values(report, values) for values in monthly
                    ]
                    for row_code, row_label in DEMO_SUPPLEMENTARY_ROWS.items():
                        for month in [*range(1, 13), None]:
                            selected = (
                                [supplementary_monthly[month - 1][row_code]]
                                if month else [item[row_code] for item in supplementary_monthly]
                            )
                            period = (
                                f"{month:02d}" if month else "YEAR"
                            ) if kind == "BUDGET" else (
                                ("A" if kind == "ACTUAL" else "F") + str(data_year)
                                + (f"M{month:02d}" if month else "")
                            )
                            pending.append(NormalizedValue(
                                upload=upload,
                                report_code=report,
                                row_code=row_code,
                                row_label=row_label,
                                period=period,
                                data_year=data_year,
                                data_kind=kind,
                                month=month,
                                unit="MONEY",
                                value_int=sum(selected),
                                source_sheet="合成演示数据（无真实项目来源）",
                                source_cell="DEMO",
                                source_formula="SUM(12 months)" if month is None else "",
                            ))
            NormalizedValue.objects.bulk_create(pending, batch_size=500)
            ProjectCycle.objects.create(project=project, cycle=cycle, current_upload=upload)
        self.stdout.write(self.style.SUCCESS(f"已生成 {year} 年演示预算及前三年对照；3 个项目，全部明确标记为演示。"))
