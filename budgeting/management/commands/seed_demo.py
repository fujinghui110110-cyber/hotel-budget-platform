import calendar
import hashlib
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from budgeting.excel.extract import sheet_slug
from budgeting.excel.ooxml import formula_manifest
from budgeting.services.workflow import (
    create_driver_adjustment,
    create_full_adjustment,
    issue_adjustment,
    project_value_details,
)
from budgeting.models import (
    AdjustmentBatch,
    AdjustmentLine,
    AuditEvent,
    BudgetCycle,
    FreezeSnapshot,
    NormalizedValue,
    ProcessingJob,
    Project,
    ProjectCycle,
    SnapshotArtifact,
    TemplateVersion,
    UploadVersion,
    ValidationIssue,
    ValidationRun,
)

PROJECTS = [
    ("P001", "滨海国际大酒店"),
    ("P002", "城市中心商务酒店"),
    ("P003", "山湖度假酒店"),
]

SUB_TABLES = [
    ("工资福利费", [("R0001", "基本工资", "MONEY"), ("R0002", "员工人数", "COUNT")]),
    ("A1客房收入(新)", [("R0001", "客房出租收入", "MONEY"), ("R0002", "出租间数", "COUNT")]),
    ("E5能耗", [("R0001", "电费", "MONEY"), ("R0002", "水费", "MONEY")]),
    ("B餐饮部汇总", [("R0001", "餐饮收入", "MONEY")]),
]


def _seed(*parts):
    return int(hashlib.md5("|".join(map(str, parts)).encode("utf-8")).hexdigest(), 16)


def _monthly_money_cents(row_label, row_code, month, salt):
    label = str(row_label)
    if "收入" in label:
        low, high = 800_000, 2_500_000
    elif "利润" in label:
        low, high = 200_000, 1_500_000
    elif "成本" in label or "薪酬" in label or "工资" in label:
        low, high = 100_000, 800_000
    elif "能源" in label or "费用" in label:
        low, high = 30_000, 300_000
    else:
        low, high = 50_000, 400_000
    return (low + (_seed(row_code, month, salt) % (high - low))) * 100


def _monthly_count(row_label, row_code, month, salt):
    label = str(row_label)
    if "房间数" in label:
        low, high = 180, 260
    elif "人数" in label or "员工" in label:
        low, high = 80, 220
    else:
        low, high = 10, 300
    return low + (_seed(row_code, month, salt) % (high - low))


def _monthly_ratio(row_label, row_code, month, salt):
    fraction = 0.55 + (_seed(row_code, month, salt) % 3500) / 10000
    return round(fraction * 10000), 10000


def _monthly_value(upload, report_code, sheet, row_code, row_label, unit, period, salt):
    common = {
        "upload": upload,
        "report_code": report_code,
        "row_code": row_code,
        "row_label": str(row_label)[:240],
        "period": period,
        "unit": unit,
        "source_sheet": sheet,
        "source_cell": "DEMO",
        "source_formula": "",
    }
    if unit == NormalizedValue.Unit.RATIO:
        num, den = _monthly_ratio(row_label, row_code, period, salt)
        return NormalizedValue(**common, value_int=0, ratio_num=num, ratio_den=den)
    if unit == NormalizedValue.Unit.COUNT:
        return NormalizedValue(**common, value_int=_monthly_count(row_label, row_code, period, salt))
    return NormalizedValue(**common, value_int=_monthly_money_cents(row_label, row_code, period, salt))


def _annual_value(upload, report_code, sheet, row_code, row_label, unit, aggregation, months):
    common = {
        "upload": upload,
        "report_code": report_code,
        "row_code": row_code,
        "row_label": str(row_label)[:240],
        "period": "YEAR",
        "unit": unit,
        "source_sheet": sheet,
        "source_cell": "PLATFORM",
    }
    if unit == NormalizedValue.Unit.RATIO:
        num = sum(int(m.ratio_num or 0) for m in months)
        den = sum(int(m.ratio_den or 0) for m in months)
        return NormalizedValue(**common, value_int=0, ratio_num=num, ratio_den=den,
                               source_formula="sum(monthly numerator) / sum(monthly denominator)")
    total = sum(int(m.value_int or 0) for m in months)
    if aggregation == "AVERAGE" and months:
        total = round(total / len(months))
    return NormalizedValue(**common, value_int=total,
                           source_formula="AVERAGE(monthly 01:12)" if aggregation == "AVERAGE" else "SUM(monthly 01:12)")


def _history_value(upload, report_code, sheet, row_code, row_label, unit, aggregation, months, period, factor):
    common = {
        "upload": upload,
        "report_code": report_code,
        "row_code": row_code,
        "row_label": str(row_label)[:240],
        "period": period,
        "unit": unit,
        "source_sheet": sheet,
        "source_cell": "DEMO",
    }
    if unit == NormalizedValue.Unit.RATIO:
        num = sum(int(m.ratio_num or 0) for m in months)
        den = sum(int(m.ratio_den or 0) for m in months)
        return NormalizedValue(**common, value_int=0, ratio_num=int(round(num * factor)), ratio_den=den,
                               source_formula="demo historical ratio")
    total = sum(int(m.value_int or 0) for m in months)
    if aggregation == "AVERAGE" and months:
        total = round(total / len(months))
    return NormalizedValue(**common, value_int=int(round(total * factor)),
                           source_formula="demo historical value")


LINKED_CODES = (
    "R0023", "R0024", "R0028", "R0029", "R0030",
    "R0031", "R0041", "R0032", "R0033", "R0101",
)


def _generate_linked_rows(upload, report_code, sheet, budget_year, meta, salt):
    """Internally-consistent linked rows: rooms → sellable → sold → room revenue
    → RevPAR → total revenue → GOP → net (costs unchanged, profit 1:1)."""
    rows = []
    rooms = 180 + (_seed("rooms", salt) % 81)  # 180-260
    occ_lo, occ_hi = 55, 90  # percent
    adr_lo, adr_hi = 40_000, 60_000  # cents/night

    monthly = {rc: [] for rc in LINKED_CODES}
    for m in range(1, 13):
        dm = calendar.monthrange(budget_year, m)[1]
        sellable_m = rooms * dm
        occ_m = occ_lo + (_seed("occ", m, salt) % (occ_hi - occ_lo + 1))
        adr_m = adr_lo + (_seed("adr", m, salt) % (adr_hi - adr_lo))
        sold_m = round(sellable_m * occ_m / 100)
        room_rev_m = sold_m * adr_m
        revpar_m = round(room_rev_m / sellable_m)
        other_m = round(room_rev_m * (0.30 + (_seed("other", m, salt) % 20) / 100.0))
        total_rev_m = room_rev_m + other_m
        gop_m = round(total_rev_m * 0.30)
        net_m = round(gop_m * 0.60)
        monthly["R0023"].append((rooms, 0, 0))
        monthly["R0024"].append((sellable_m, 0, 0))
        monthly["R0028"].append((sold_m, 0, 0))
        monthly["R0029"].append((0, sold_m, sellable_m))
        monthly["R0030"].append((adr_m, 0, 0))
        monthly["R0031"].append((revpar_m, 0, 0))
        monthly["R0041"].append((room_rev_m, 0, 0))
        monthly["R0032"].append((total_rev_m, 0, 0))
        monthly["R0033"].append((gop_m, 0, 0))
        monthly["R0101"].append((net_m, 0, 0))

    sold_y = sum(v[0] for v in monthly["R0028"])
    sellable_y = sum(v[0] for v in monthly["R0024"])
    room_rev_y = sum(v[0] for v in monthly["R0041"])
    year = {
        "R0023": (rooms, 0, 0),
        "R0024": (sellable_y, 0, 0),
        "R0028": (sold_y, 0, 0),
        "R0029": (0, sold_y, sellable_y),
        "R0030": (round(room_rev_y / sold_y) if sold_y else 0, 0, 0),
        "R0031": (round(room_rev_y / sellable_y) if sellable_y else 0, 0, 0),
        "R0041": (room_rev_y, 0, 0),
        "R0032": (sum(v[0] for v in monthly["R0032"]), 0, 0),
        "R0033": (sum(v[0] for v in monthly["R0033"]), 0, 0),
        "R0101": (sum(v[0] for v in monthly["R0101"]), 0, 0),
    }

    def _nv(row_code, period, value_int, ratio_num, ratio_den, source_formula=""):
        label, unit, _agg = meta[row_code]
        if unit != NormalizedValue.Unit.RATIO:
            ratio_num = ratio_den = None
        return NormalizedValue(
            upload=upload, report_code=report_code, row_code=row_code,
            row_label=str(label)[:240], period=period, unit=unit,
            value_int=value_int, ratio_num=ratio_num, ratio_den=ratio_den,
            source_sheet=sheet, source_cell="DEMO", source_formula=source_formula,
        )

    for rc in LINKED_CODES:
        for m in range(1, 13):
            vi, rn, rd = monthly[rc][m - 1]
            rows.append(_nv(rc, f"{m:02d}", vi, rn, rd))
        vi, rn, rd = year[rc]
        rows.append(_nv(rc, "YEAR", vi, rn, rd, "SUM(monthly 01:12)"))
        _label, unit, _agg = meta[rc]
        for factor, period in ((0.82, f"A{budget_year - 2}"), (0.92, f"F{budget_year - 1}")):
            if unit == NormalizedValue.Unit.RATIO:
                rows.append(_nv(rc, period, 0, round(rn * factor), rd))
            else:
                rows.append(_nv(rc, period, round(vi * factor), rn, rd))
    return rows


def _generate_values(upload, manifest, budget_year):
    rows = []
    for report_code, report in (manifest.get("reports") or {}).items():
        sheet = report.get("sheet", report_code)
        mapping = report.get("mapping") or report.get("cells") or []
        unique = {}
        for item in mapping:
            unit = (item.get("unit") or NormalizedValue.Unit.MONEY).upper()
            if unit not in {NormalizedValue.Unit.MONEY, NormalizedValue.Unit.COUNT, NormalizedValue.Unit.RATIO}:
                unit = NormalizedValue.Unit.MONEY
            aggregation = str(item.get("aggregation") or "SUM").upper()
            key = (item["row_code"], str(item.get("row_label") or item["row_code"]), unit, aggregation)
            unique.setdefault(key, item)
        salt = str(upload.project.code)
        linked_meta = {k[0]: (k[1], k[2], k[3]) for k in unique if k[0] in LINKED_CODES}
        linked_ready = set(linked_meta) >= set(LINKED_CODES)
        if linked_ready:
            rows.extend(_generate_linked_rows(upload, report_code, sheet, budget_year, linked_meta, salt))
        for (row_code, row_label, unit, aggregation), item in unique.items():
            if linked_ready and row_code in LINKED_CODES:
                continue
            months = [
                _monthly_value(upload, report_code, sheet, row_code, row_label, unit, f"{m:02d}", salt)
                for m in range(1, 13)
            ]
            rows.extend(months)
            rows.append(_annual_value(upload, report_code, sheet, row_code, row_label, unit, aggregation, months))
            rows.append(_history_value(upload, report_code, sheet, row_code, row_label, unit, aggregation, months, f"A{budget_year - 2}", 0.82))
            rows.append(_history_value(upload, report_code, sheet, row_code, row_label, unit, aggregation, months, f"F{budget_year - 1}", 0.92))
    return rows


def _generate_sub_table_values(upload, sub_tables):
    rows = []
    salt = str(upload.project.code)
    for sheet, entries in sub_tables:
        code = sheet_slug(sheet)
        for row_code, row_label, unit in entries:
            months = [
                _monthly_value(upload, code, sheet, row_code, row_label, unit, f"{m:02d}", salt)
                for m in range(1, 13)
            ]
            rows.extend(months)
            rows.append(_annual_value(upload, code, sheet, row_code, row_label, unit, "SUM", months))
    return rows


def _wipe():
    # Delete in dependency-safe order so PROTECT foreign keys never block us.
    SnapshotArtifact.objects.all().delete()
    FreezeSnapshot.objects.all().delete()
    AdjustmentLine.objects.all().delete()
    AdjustmentBatch.objects.all().delete()
    ValidationIssue.objects.all().delete()
    ValidationRun.objects.all().delete()
    ProcessingJob.objects.all().delete()
    ProjectCycle.objects.all().delete()
    NormalizedValue.objects.all().delete()
    UploadVersion.objects.all().delete()
    AuditEvent.objects.all().delete()
    BudgetCycle.objects.all().delete()
    get_user_model().objects.all().delete()
    Project.objects.all().delete()


class Command(BaseCommand):
    help = "Reset and seed the local MVP demo: 3 approved projects, users, cycle, and values."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("正式环境禁止创建演示账号，请使用 createsuperuser 和组织账号管理。")
        settings.BUDGET_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
        _wipe()

        template_path = settings.BASE_DIR / "artifacts" / "平台标准预算模板_V1.xlsx"
        manifest_path = settings.BASE_DIR / "artifacts" / "template_manifest.json"
        digest = "pending"
        if template_path.exists():
            _, digest = formula_manifest(template_path)
        template, _ = TemplateVersion.objects.update_or_create(
            version="V1",
            defaults={
                "budget_year": 2026,
                "file_path": str(template_path.relative_to(settings.BASE_DIR)) if template_path.exists() else "",
                "manifest_path": str(manifest_path.relative_to(settings.BASE_DIR)) if manifest_path.exists() else "",
                "formula_manifest_hash": digest,
                "rule_version": "rules-v1",
                "is_active": template_path.exists(),
            },
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

        cycle = BudgetCycle.objects.create(
            budget_year=2026, revision_no=1, name="2026预算", status=BudgetCycle.Status.OPEN,
        )

        User = get_user_model()
        admin = User.objects.create(username="admin", role="ADMIN", is_staff=True, is_superuser=True)
        admin.set_password("admin123")
        admin.save()

        now = timezone.now()
        for code, name in PROJECTS:
            project = Project.objects.create(code=code, name=name)
            user = User.objects.create(username=f"project_{code.lower()}", role="PROJECT", project=project)
            user.set_password("project123")
            user.save()

            upload = UploadVersion.objects.create(
                project=project,
                cycle=cycle,
                template=template,
                status=UploadVersion.Status.APPROVED,
                original_name=f"{name}-2026预算套表.xlsx",
                original_path=f"storage/demo/{code}/2026预算套表.xlsx",
                sha256=hashlib.sha256(f"demo:{code}:2026".encode("utf-8")).hexdigest(),
                submitted_at=now,
                approved_at=now,
            )
            NormalizedValue.objects.bulk_create(_generate_values(upload, manifest, cycle.budget_year))
            NormalizedValue.objects.bulk_create(_generate_sub_table_values(upload, SUB_TABLES))
            ProjectCycle.objects.create(project=project, cycle=cycle, current_upload=upload, is_open=True)
            if code == "P002":
                batch = create_driver_adjustment(
                    cycle, project, "PL_TOTAL_WINE", "OCC", 72,
                    "演示：调整出租率至 72%，联动测算客房收入与利润。",
                )
                issue_adjustment(batch)
            if code == "P003":
                details = project_value_details(project, cycle, "PL_TOTAL_WINE")
                by_row = {}
                for (rc, period), d in details.items():
                    by_row.setdefault(rc, {})[period] = d

                def _num(d):
                    return d["ratio_num"] / d["ratio_den"] if d.get("ratio_den") else (d.get("value_int") or 0)

                edits = {
                    "R0041": {"YEAR": int(round(_num(by_row["R0041"]["YEAR"]) * 1.10))},
                    "R0077": {"01": int(round(_num(by_row["R0077"]["01"]) * 0.90))},
                }
                batch = create_full_adjustment(
                    cycle, project, "PL_TOTAL_WINE", edits,
                    "演示：全表调整——客房收入上调 10%、一月能耗下调 10%。",
                )
                issue_adjustment(batch)

        self.stdout.write(self.style.SUCCESS(
            "ready admin/admin123; project_p001..project_p003 / project123; 3 approved projects"
        ))
