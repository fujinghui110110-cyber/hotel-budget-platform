from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from budgeting.models import NormalizedValue, Project, ProjectCycle, UploadVersion
from budgeting.services.metrics import METRICS, get_metric, metric_for_row, rolling_years


KIND_LABELS = {"ACTUAL": "实际", "FORECAST": "预测", "BUDGET": "预算"}
MONTHS = tuple(range(1, 13))
_HISTORY_RE = re.compile(r"^([AFB])\s*(20\d{2})(?:[-_/]?M?([01]\d))?$", re.I)
_YEAR_MONTH_RE = re.compile(r"^(20\d{2})[-_/]?M?([01]\d)$", re.I)
DISPLAY_UNITS = ("yuan", "wan")
DISPLAY_UNIT_LABELS = {"yuan": "元", "wan": "万元"}


def approved_current_uploads(cycle, project_id=None):
    if not cycle:
        return []
    qs = (
        ProjectCycle.objects.filter(
            cycle=cycle,
            current_upload__cycle=cycle,
            current_upload__status=UploadVersion.Status.APPROVED,
            project__is_active=True,
        )
        .select_related("project", "current_upload")
        .order_by("project__code")
    )
    if project_id:
        qs = qs.filter(project_id=project_id)
    return list(qs)


def _dimension(value):
    year = value.data_year
    kind = (value.data_kind or "").upper() or None
    month = value.month
    period = (value.period or "").strip().upper()
    if year is not None and kind:
        return int(year), kind, int(month) if month is not None else None

    match = _HISTORY_RE.fullmatch(period)
    if match:
        kind = {"A": "ACTUAL", "F": "FORECAST", "B": "BUDGET"}[match.group(1).upper()]
        return int(match.group(2)), kind, int(match.group(3)) if match.group(3) else None
    match = _YEAR_MONTH_RE.fullmatch(period)
    if match:
        return int(match.group(1)), kind or "BUDGET", int(match.group(2))
    if period.isdigit() and 1 <= int(period) <= 12:
        return int(year or value.upload.cycle.budget_year), kind or "BUDGET", int(period)
    if period in {"YEAR", "FY", "ANNUAL", "全年", "全年合计"} or not period:
        return int(year or value.upload.cycle.budget_year), kind or "BUDGET", None
    return (int(year), kind, int(month) if month is not None else None) if year and kind else (None, None, None)


def _metric_for(metric_code, report_code):
    metric = get_metric(metric_code)
    if metric:
        return metric
    metric = metric_for_row(report_code, metric_code)
    return metric or get_metric("revenue_total")


def _row_for(metric, field, report_code):
    rows = metric.get(field) or {}
    return rows.get(report_code)


def _numeric(value):
    if value.ratio_num is not None and value.ratio_den is not None:
        return int(value.ratio_num), int(value.ratio_den)
    return int(value.value_int or 0), None


def _combine_values(values, aggregation, unit):
    if not values:
        return None
    if aggregation == "RATIO":
        nums = dens = 0
        for item in values:
            num, den = _numeric(item)
            if den is None:
                return None
            nums += num
            dens += den
        if dens == 0:
            return None
        return {
            "value": _round_ratio(nums, dens),
            "value_int": _round_ratio(nums, dens),
            "ratio_num": nums,
            "ratio_den": dens,
            "unit": unit,
        }
    if aggregation == "DERIVED":
        nums = dens = 0
        for item in values:
            num, den = _numeric(item)
            nums += num
            if den is None:
                return None
            dens += den
        if dens == 0:
            return None
        value = int((Decimal(nums) / Decimal(dens)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return {"value": value, "value_int": value, "ratio_num": nums, "ratio_den": dens, "unit": unit}
    if aggregation == "AVERAGE":
        total = sum(int(item.value_int or 0) for item in values)
        value = int((Decimal(total) / Decimal(len(values))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return {"value": value, "value_int": value, "ratio_num": None, "ratio_den": None, "unit": unit}
    value = sum(int(item.value_int or 0) for item in values)
    return {"value": value, "value_int": value, "ratio_num": None, "ratio_den": None, "unit": unit}


def _round_ratio(num, den):
    if not den:
        return None
    return int((Decimal(num) * Decimal(10_000) / Decimal(den)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def normalize_display_unit(metric, display_unit=None, default="yuan"):
    if metric.get("unit") != "MONEY":
        return "native"
    if (metric.get("aggregation") or "").upper() == "DERIVED":
        return "yuan"
    return display_unit if display_unit in DISPLAY_UNITS else default


def _metric_unit_label(metric, display_unit="yuan", metric_code=None):
    unit = metric.get("unit")
    if unit == "RATIO":
        return "%"
    if unit == "COUNT":
        return "数量"
    if unit == "MONEY" and (metric.get("aggregation") or "").upper() == "DERIVED":
        if metric_code in {"adr", "revpar"}:
            return "元/房晚"
        if metric_code == "cost_room_operating_per_room":
            return "元/房晚"
        return "元"
    return DISPLAY_UNIT_LABELS.get(display_unit, "元")


def _display_value(value, unit, display_unit="yuan", metric_code=None, aggregation=None):
    if value is None:
        return "—"
    if unit == "MONEY":
        if (aggregation or "").upper() == "DERIVED":
            suffix = _metric_unit_label({"unit": unit, "aggregation": aggregation}, "yuan", metric_code)
            return f"{Decimal(value) / Decimal(100):,.2f} {suffix}"
        divisor = Decimal(1_000_000) if display_unit == "wan" else Decimal(100)
        return f"{Decimal(value) / divisor:,.2f} {DISPLAY_UNIT_LABELS.get(display_unit, '元')}"
    if unit == "RATIO":
        return f"{Decimal(value) / Decimal(100):,.2f}%"
    return f"{int(value):,}"


def _chart_value(value, unit, display_unit="yuan", aggregation=None):
    if value is None:
        return None
    if unit == "MONEY":
        if (aggregation or "").upper() == "DERIVED":
            divisor = Decimal(100)
        else:
            divisor = Decimal(1_000_000) if display_unit == "wan" else Decimal(100)
    elif unit == "RATIO":
        divisor = Decimal(100)
    else:
        divisor = Decimal(1)
    return float(Decimal(value) / divisor)


def _project_metric(rows, metric, report_code, dimension):
    year, kind, month = dimension
    by_row = defaultdict(list)
    for row in rows:
        if _dimension(row) == dimension:
            by_row[row.row_code].append(row)

    aggregation = (metric.get("aggregation") or "SUM").upper()
    unit = metric.get("unit") or NormalizedValue.Unit.MONEY
    target = _row_for(metric, "rows", report_code)
    if aggregation in {"RATIO", "DERIVED"}:
        numerator = _row_for(metric, "numerator_rows", report_code)
        denominator = _row_for(metric, "denominator_rows", report_code)
        if numerator and denominator:
            nums = _component_total(by_row.get(numerator, []))
            dens = _component_total(by_row.get(denominator, []))
            if nums is not None and dens is not None and dens[1] == 0 and denominator != numerator:
                return None
            if nums is not None and dens is not None:
                n = nums[0] if nums[1] is None else nums[0]
                d = dens[0] if dens[1] is None else dens[0]
                if not d:
                    return None
                value = _round_ratio(n, d) if aggregation == "RATIO" else int((Decimal(n) / Decimal(d)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                return {"value": value, "value_int": value, "ratio_num": n, "ratio_den": d, "unit": unit}
        target_values = by_row.get(target, [])
        direct = _combine_values(target_values, aggregation, unit)
        if direct is None and month is None and len(target_values) == 1 and target_values[0].value_int is not None:
            value = int(target_values[0].value_int)
            return {"value": value, "value_int": value, "ratio_num": None, "ratio_den": None, "unit": unit}
        return direct
    return _combine_values(by_row.get(target, []), aggregation, unit)


def _component_total(values):
    if not values:
        return None
    total = 0
    raw_den = None
    for item in values:
        if item.value_int is None and item.ratio_num is None:
            return None
        num, den = _numeric(item)
        total += num
        if den is not None:
            raw_den = (raw_den or 0) + den
    return total, raw_den


def _aggregate(uploads, rows_by_upload, metric, report_code, dimension):
    values = []
    project_ids = []
    for pc in uploads:
        value = _project_metric(rows_by_upload.get(pc.current_upload_id, []), metric, report_code, dimension)
        if value is not None:
            values.append(value)
            project_ids.append(pc.project_id)
    if not values:
        return {
            "value": None,
            "value_int": None,
            "ratio_num": None,
            "ratio_den": None,
            "included_projects": 0,
            "project_ids": [],
        }
    aggregation = (metric.get("aggregation") or "SUM").upper()
    if aggregation == "RATIO":
        num = sum(int(item.get("ratio_num") or 0) for item in values)
        den = sum(int(item.get("ratio_den") or 0) for item in values)
        resolved = None if not den else _round_ratio(num, den)
        return {"value": resolved, "value_int": resolved, "ratio_num": num, "ratio_den": den, "included_projects": len(project_ids), "project_ids": project_ids}
    if aggregation == "DERIVED":
        if all(item.get("ratio_den") is not None for item in values):
            num = sum(int(item["ratio_num"] or 0) for item in values)
            den = sum(int(item["ratio_den"] or 0) for item in values)
            resolved = None if not den else int((Decimal(num) / Decimal(den)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            return {"value": resolved, "value_int": resolved, "ratio_num": num, "ratio_den": den, "included_projects": len(project_ids), "project_ids": project_ids}
        if len(values) == 1:
            resolved = values[0].get("value_int")
            return {"value": resolved, "value_int": resolved, "ratio_num": None, "ratio_den": None, "included_projects": len(project_ids), "project_ids": project_ids}
        return {"value": None, "value_int": None, "ratio_num": None, "ratio_den": None, "included_projects": 0, "project_ids": []}
    if aggregation == "AVERAGE":
        resolved = int((Decimal(sum(int(item["value_int"] or 0) for item in values)) / Decimal(len(values))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    else:
        resolved = sum(int(item["value_int"] or 0) for item in values)
    return {"value": resolved, "value_int": resolved, "ratio_num": None, "ratio_den": None, "included_projects": len(project_ids), "project_ids": project_ids}


def aggregate_metric(cycle, metric_code, report_code="PL_TOTAL_WINE", year=None, kind=None, month=None, project_id=None):
    metric = _metric_for(metric_code, report_code)
    if not cycle:
        return {"value": None, "value_int": None, "ratio_num": None, "ratio_den": None, "included_projects": 0, "project_ids": [], "year": year, "kind": kind, "month": month, "unit": metric.get("unit"), "metric": metric_code, "report_code": report_code}
    year = int(year or cycle.budget_year)
    kind = (kind or "BUDGET").upper()
    uploads = approved_current_uploads(cycle, project_id=project_id)
    rows_by_upload = _load_rows(uploads, metric, report_code)
    result = _aggregate(uploads, rows_by_upload, metric, report_code, (year, kind, month))
    result.update({"year": year, "kind": kind, "month": month, "unit": metric.get("unit"), "metric": metric_code, "report_code": report_code})
    return result


def _load_rows(uploads, metric, report_code):
    upload_ids = [pc.current_upload_id for pc in uploads]
    if not upload_ids:
        return {}
    codes = set()
    for field in ("rows", "numerator_rows", "denominator_rows"):
        code = _row_for(metric, field, report_code)
        if code:
            codes.add(code)
    if not codes:
        return {}
    rows = NormalizedValue.objects.filter(upload_id__in=upload_ids, report_code=report_code, row_code__in=codes).select_related("upload__cycle")
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.upload_id].append(row)
    return grouped


def build_trend(cycle, metric_code="revenue_total", report_code="PL_TOTAL_WINE", project_id=None, display_unit=None):
    metric = _metric_for(metric_code, report_code)
    display_unit = normalize_display_unit(metric, display_unit)
    uploads = approved_current_uploads(cycle, project_id=project_id)
    rows_by_upload = _load_rows(uploads, metric, report_code)
    active_count = Project.objects.filter(is_active=True).count()
    if project_id:
        active_count = Project.objects.filter(pk=project_id, is_active=True).count()
    periods = rolling_years(cycle.budget_year) if cycle else []
    series = []
    annual = []
    for year, kind in periods:
        monthly = [_aggregate(uploads, rows_by_upload, metric, report_code, (year, kind, month)) for month in MONTHS]
        annual_bucket = _aggregate(uploads, rows_by_upload, metric, report_code, (year, kind, None))
        monthly_values = [item["value"] for item in monthly]
        monthly_available = any(item["value"] is not None for item in monthly)
        annual_only = annual_bucket["value"] is not None and not monthly_available
        if annual_bucket["value"] is None and all(item["value"] is not None for item in monthly):
            annual_bucket = _rollup_months(monthly, metric)
            annual_bucket["source"] = "monthly_rollup"
        label = f"{year}{KIND_LABELS.get(kind, kind)}"
        annual_label = f"{label}（仅年度）" if annual_only else label
        coverage = [{"included": item["included_projects"], "active": active_count} for item in monthly]
        annual_item = {
            "year": year,
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "label": annual_label,
            "value": annual_bucket["value"],
            "value_int": annual_bucket["value_int"],
            "display": _display_value(annual_bucket["value"], metric.get("unit"), display_unit, metric_code, metric.get("aggregation")),
            "chart_value": _chart_value(annual_bucket["value"], metric.get("unit"), display_unit, metric.get("aggregation")),
            "ratio_num": annual_bucket.get("ratio_num"),
            "ratio_den": annual_bucket.get("ratio_den"),
            "included_projects": annual_bucket["included_projects"],
            "active_projects": active_count,
            "coverage": {"included": annual_bucket["included_projects"], "active": active_count},
            "annual_only": annual_only,
        }
        annual.append(annual_item)
        series.append({
            "key": f"{year}-{kind}",
            "name": label,
            "label": annual_label,
            "year": year,
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "values": monthly_values,
            "data": monthly_values,
            "months": [f"{month:02d}" for month in MONTHS],
            "annual": annual_bucket["value"],
            "annual_display": _display_value(annual_bucket["value"], metric.get("unit"), display_unit, metric_code, metric.get("aggregation")),
            "annual_chart_value": _chart_value(annual_bucket["value"], metric.get("unit"), display_unit, metric.get("aggregation")),
            "annual_only": annual_only,
            "coverage": coverage,
            "included_projects": max((item["included_projects"] for item in monthly), default=annual_bucket["included_projects"]),
            "active_projects": active_count,
            "display_values": [_display_value(item["value"], metric.get("unit"), display_unit, metric_code, metric.get("aggregation")) for item in monthly],
            "chart_values": [_chart_value(item["value"], metric.get("unit"), display_unit, metric.get("aggregation")) for item in monthly],
        })
    return {
        "metric": metric_code,
        "metric_label": metric.get("label", metric_code),
        "group": metric.get("group"),
        "unit": metric.get("unit"),
        "unit_label": _metric_unit_label(metric, display_unit, metric_code),
        "display_unit": display_unit,
        "aggregation": metric.get("aggregation"),
        "report_code": report_code,
        "budget_year": cycle.budget_year if cycle else None,
        "years": [{"year": year, "kind": kind, "kind_label": KIND_LABELS.get(kind, kind), "label": f"{year}{KIND_LABELS.get(kind, kind)}"} for year, kind in periods],
        "months": [f"{month:02d}" for month in MONTHS],
        "series": series,
        "annual": annual,
        "active_project_count": active_count,
        "included_project_count": len(uploads),
        "coverage": {"included": len(uploads), "active": active_count},
        "project_ids": [pc.project_id for pc in uploads],
        "project_codes": [pc.project.code for pc in uploads],
    }


def _detail_source_codes(metric, report_code):
    codes = []
    for field in ("rows", "numerator_rows", "denominator_rows"):
        code = _row_for(metric, field, report_code)
        if code and code not in codes:
            codes.append(code)
    return codes


def _source_detail(row):
    return {
        "value_id": row.pk,
        "row_code": row.row_code,
        "row_label": row.row_label or row.row_code,
        "period": row.period,
        "data_year": row.data_year,
        "data_kind": row.data_kind,
        "month": row.month,
        "unit": row.unit,
        "value": row.value_int,
        "value_raw": _exact_value(row.value_int),
        "display": _display_value(row.value_int, row.unit, "yuan", None, "SUM"),
        "ratio_num": row.ratio_num,
        "ratio_den": row.ratio_den,
        "source_sheet": row.source_sheet,
        "source_cell": row.source_cell,
        "source_formula": row.source_formula or "",
        "upload_id": str(row.upload_id),
        "upload_name": row.upload.original_name or row.upload.original_path,
        "upload_created_at": row.upload.created_at.isoformat() if row.upload.created_at else None,
    }


def build_drilldown(cycle, metric_code="revenue_total", report_code="PL_TOTAL_WINE", year=None, kind=None, month=None, project_id=None, display_unit=None):
    metric = _metric_for(metric_code, report_code)
    display_unit = normalize_display_unit(metric, display_unit)
    if not cycle:
        return {"metric": metric_code, "report_code": report_code, "year": year, "kind": kind, "month": month, "projects": [], "coverage": {"included": 0, "active": 0}}
    year = int(year or cycle.budget_year)
    kind = (kind or "BUDGET").upper()
    month = int(month) if month not in (None, "") else None
    if month is not None and not 1 <= month <= 12:
        month = None
    dimension = (year, kind, month)
    uploads = approved_current_uploads(cycle, project_id=project_id)
    rows_by_upload = _load_rows(uploads, metric, report_code)
    codes = set(_detail_source_codes(metric, report_code))
    projects = []
    for pc in uploads:
        rows = rows_by_upload.get(pc.current_upload_id, [])
        bucket = _project_metric(rows, metric, report_code, dimension)
        source_dimensions = [dimension]
        if month is None and bucket is None:
            monthly = [_project_metric(rows, metric, report_code, (year, kind, item_month)) for item_month in MONTHS]
            if all(item and item.get("value") is not None for item in monthly):
                bucket = _rollup_months(monthly, metric)
                source_dimensions = [(year, kind, item_month) for item_month in MONTHS]
        source_rows = [row for row in rows if row.row_code in codes and _dimension(row) in source_dimensions]
        source_rows.sort(key=lambda row: (row.month is None, row.month or 0, row.row_code))
        value = bucket.get("value") if bucket else None
        projects.append({
            "project_id": pc.project_id,
            "project_code": pc.project.code,
            "project_name": pc.project.name,
            "upload_id": str(pc.current_upload_id),
            "upload_name": pc.current_upload.original_name or pc.current_upload.original_path,
            "upload_created_at": pc.current_upload.created_at.isoformat() if pc.current_upload.created_at else None,
            "value": value,
            "value_raw": _exact_value(value),
            "display": _display_value(value, metric.get("unit"), display_unit, metric_code, metric.get("aggregation")),
            "unit": metric.get("unit"),
            "unit_label": _metric_unit_label(metric, display_unit, metric_code),
            "included": value is not None,
            "status": "有值" if value is not None else "缺少指标或必要分母",
            "ratio_num": bucket.get("ratio_num") if bucket else None,
            "ratio_den": bucket.get("ratio_den") if bucket else None,
            "sources": [_source_detail(row) for row in source_rows],
        })
    included = sum(1 for item in projects if item["included"])
    active_count = Project.objects.filter(is_active=True).count()
    if project_id:
        active_count = Project.objects.filter(pk=project_id, is_active=True).count()
    return {
        "metric": metric_code,
        "metric_label": metric.get("label", metric_code),
        "report_code": report_code,
        "year": year,
        "kind": kind,
        "month": month,
        "period_label": f"{year}{KIND_LABELS.get(kind, kind)}" + (f" · {month:02d}月" if month else " · 年度"),
        "unit": metric.get("unit"),
        "unit_label": _metric_unit_label(metric, display_unit, metric_code),
        "display_unit": display_unit,
        "projects": projects,
        "coverage": {"included": included, "active": active_count},
    }


def _rollup_months(monthly, metric):
    aggregation = (metric.get("aggregation") or "SUM").upper()
    included = set()
    for item in monthly:
        included.update(item.get("project_ids", []))
    if aggregation == "RATIO":
        num = sum(int(item.get("ratio_num") or 0) for item in monthly)
        den = sum(int(item.get("ratio_den") or 0) for item in monthly)
        value = None if not den else _round_ratio(num, den)
        return {"value": value, "value_int": value, "ratio_num": num, "ratio_den": den, "included_projects": len(included), "project_ids": list(included)}
    if aggregation == "DERIVED":
        num = sum(int(item.get("ratio_num") or 0) for item in monthly)
        den = sum(int(item.get("ratio_den") or 0) for item in monthly)
        value = None if not den else int((Decimal(num) / Decimal(den)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return {"value": value, "value_int": value, "ratio_num": num, "ratio_den": den, "included_projects": len(included), "project_ids": list(included)}
    value = sum(int(item["value"] or 0) for item in monthly)
    return {"value": value, "value_int": value, "ratio_num": None, "ratio_den": None, "included_projects": len(included), "project_ids": list(included)}


def trend_payload(cycle, metric_code="revenue_total", report_code="PL_TOTAL_WINE", project_id=None, display_unit=None):
    return build_trend(cycle, metric_code, report_code, project_id, display_unit)


def metric_choices(report_code="PL_TOTAL_WINE"):
    choices = []
    for code, spec in METRICS.items():
        row_code = _row_for(spec, "rows", report_code)
        aggregation = (spec.get("aggregation") or "SUM").upper()
        if aggregation in {"RATIO", "DERIVED"}:
            numerator = _row_for(spec, "numerator_rows", report_code)
            denominator = _row_for(spec, "denominator_rows", report_code)
            available = bool(numerator and denominator)
            row_code = row_code or numerator
        else:
            available = bool(row_code)
        choices.append({
            "code": code,
            "label": spec.get("label", code),
            "group": spec.get("group"),
            "row_code": row_code,
            "unit": spec.get("unit"),
            "available": available,
        })
    return choices


def _exact_value(value):
    return None if value is None else str(int(value))


def export_payload(payload):
    result = dict(payload)
    result["annual"] = []
    for item in payload.get("annual", []):
        exported = dict(item)
        exported["value"] = _exact_value(item.get("value"))
        exported["value_int"] = _exact_value(item.get("value_int"))
        result["annual"].append(exported)
    result["series"] = []
    for item in payload.get("series", []):
        exported = dict(item)
        exported["values"] = [_exact_value(value) for value in item.get("values", [])]
        exported["data"] = [_exact_value(value) for value in item.get("data", item.get("values", []))]
        exported["annual"] = _exact_value(item.get("annual"))
        result["series"].append(exported)
    return result


def flatten_export(payload):
    rows = []
    for item in payload.get("annual", []):
        rows.append({"scope": "annual", "label": item["label"], "year": item["year"], "kind": item["kind"], "month": "", "value": _exact_value(item["value"]), "included_projects": item["included_projects"], "active_projects": item["active_projects"], "annual_only": item["annual_only"]})
    for item in payload.get("series", []):
        for month, value, coverage in zip(item["months"], item["values"], item["coverage"]):
            rows.append({"scope": "monthly", "label": item["label"], "year": item["year"], "kind": item["kind"], "month": month, "value": _exact_value(value), "included_projects": coverage["included"], "active_projects": coverage["active"], "annual_only": item["annual_only"]})
    return rows


__all__ = [
    "KIND_LABELS",
    "MONTHS",
    "aggregate_metric",
    "approved_current_uploads",
    "build_trend",
    "export_payload",
    "flatten_export",
    "metric_choices",
    "normalize_display_unit",
    "trend_payload",
]
