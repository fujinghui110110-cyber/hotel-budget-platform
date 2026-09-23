import calendar
import json
from collections import OrderedDict
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from django.conf import settings
from django.db import transaction

from budgeting.excel.business_checks import TOTAL_RULES, ZZ_RULES
from budgeting.excel.money import yuan_to_cents
from budgeting.models import (
    BudgetCycle,
    BudgetScenario,
    NormalizedValue,
    Project,
    ProjectCycle,
    REPORTS,
    UploadVersion,
)
from budgeting.services.allocations import largest_remainder
from budgeting.services.pnl_graph import CROSS_REF, _eval, _topo_order, build_report_graph, load_external_values, parse_formula
from budgeting.services.template_paths import resolve_template_path

try:
    from budgeting.services.metrics import METRICS, REPORT_CODES
except ImportError:
    METRICS = {}
    REPORT_CODES = tuple(REPORTS)


RULE_VERSION = "FIXED_COST_V2"
MONTHS = tuple(f"{i:02d}" for i in range(1, 13))
REPORT_CODES = tuple(REPORT_CODES or REPORTS)
DRIVER_LABELS = {
    "ADR": "平均房价 ADR",
    "OCC": "出租率 OCC",
    "ROOMS": "房间数",
    "ROOM_REV": "客房收入",
}
DRIVERS = tuple(DRIVER_LABELS)
COST_MODE_LABELS = {
    "fixed": "固定成本（保持现有口径）",
    "proportional": "客房成本按本预算收入率联动",
}
COST_MODES = tuple(COST_MODE_LABELS)
FINAL_PROFIT_ROWS = {
    "PL_TOTAL_WINE": "R0101",
    "PL_TOTAL_NOWINE": "R0101",
    "PL_ZZ_WINE": "R0127",
    "PL_ZZ_NOWINE": "R0127",
}
FALLBACK_ROWS = {
    "PL_TOTAL_WINE": {
        "rooms": "R0023", "sellable": "R0024", "sold": "R0028", "occ": "R0029",
        "adr": "R0030", "revpar": "R0031", "room_rev": "R0041", "total": "R0032",
        "gop": "R0033", "operating": "R0090", "npi": "R0105", "final": "R0101",
    },
    "PL_TOTAL_NOWINE": {
        "rooms": "R0023", "sellable": "R0024", "sold": "R0028", "occ": "R0029",
        "adr": "R0030", "revpar": "R0031", "room_rev": "R0041", "total": "R0032",
        "gop": "R0033", "operating": "R0090", "npi": "R0105", "final": "R0101",
    },
    "PL_ZZ_WINE": {
        "rooms": "R0009", "sellable": "R0010", "sold": "R0011", "occ": "R0012",
        "adr": "R0013", "revpar": "R0014", "room_rev": "R0021", "total": "R0015",
        "gop": "R0016", "operating": "R0113", "npi": "R0129", "final": "R0127",
    },
    "PL_ZZ_NOWINE": {
        "rooms": "R0009", "sellable": "R0010", "sold": "R0011", "occ": "R0012",
        "adr": "R0013", "revpar": "R0014", "room_rev": "R0021", "total": "R0015",
        "gop": "R0016", "operating": "R0113", "npi": "R0129", "final": "R0127",
    },
}
METRIC_KEYS = {
    "total": "revenue_total", "room_rev": "revenue_room", "gop": "profit_gop",
    "operating": "profit_operating", "npi": "profit_npi", "occ": "occ",
    "adr": "adr", "revpar": "revpar",
}
REQUIRED_KEYS = ("room_rev", "sellable", "sold", "adr", "total", "gop", "operating", "npi", "final")


def _profit_rules(report_code):
    return ZZ_RULES if str(report_code).startswith("PL_ZZ") else TOTAL_RULES


def _profit_output_targets(report_code):
    if str(report_code).startswith("PL_ZZ"):
        return {"total": 15, "gop": 101, "operating": 113, "npi": 129, "final": 127}
    return {"total": 32, "gop": 80, "operating": 90, "npi": 105, "final": 101}


class ScenarioError(ValueError):
    pass


def _decimal(value):
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise ScenarioError(f"数值无效：{value}")


def _integer(value):
    number = _decimal(value)
    rounded = number.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if rounded != number:
        raise ScenarioError(f"必须为整数：{value}")
    return int(rounded)


def _round_int(value):
    return int(_decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _period(period):
    text = str(period or "").upper()
    if text in {"FY", "YEAR", "ANNUAL"}:
        return "YEAR"
    if text.isdigit() and 1 <= int(text) <= 12:
        return f"{int(text):02d}"
    return text


def _cell(value, unit=None, *, ratio_num=None, ratio_den=None):
    if isinstance(value, Mapping):
        result = dict(value)
        result["unit"] = str(result.get("unit") or unit or "MONEY")
        result["value_int"] = int(result.get("value_int") or 0)
        if result["unit"] == "RATIO":
            result["ratio_num"] = int(result.get("ratio_num") or 0)
            result["ratio_den"] = int(result.get("ratio_den") or 0)
        return result
    unit = str(unit or "MONEY")
    if unit == "RATIO":
        return {"value_int": _round_int(_decimal(value) * 10_000), "unit": unit,
                "ratio_num": int(ratio_num or 0), "ratio_den": int(ratio_den or 0)}
    return {"value_int": int(value or 0), "unit": unit, "ratio_num": None, "ratio_den": None}


def _numeric(cell):
    if not cell:
        return Decimal("0")
    if str(cell.get("unit")) == "RATIO":
        denominator = int(cell.get("ratio_den") or 0)
        if denominator:
            return _decimal(cell.get("ratio_num") or 0) / denominator
        return _decimal(cell.get("value_int") or 0) / 10_000
    return _decimal(cell.get("value_int") or 0)


def _number_cell(value, unit, *, numerator=None, denominator=None):
    if unit == "RATIO":
        numerator = int(numerator or 0)
        denominator = int(denominator or 0)
        ratio = _decimal(numerator) / denominator if denominator else Decimal("0")
        return {"value_int": _round_int(ratio * 10_000), "unit": unit,
                "ratio_num": numerator, "ratio_den": denominator}
    return {"value_int": _round_int(value), "unit": unit, "ratio_num": None, "ratio_den": None}


def allocate_annual_room_revenue(target_cents, monthly_weights):
    if isinstance(monthly_weights, Mapping):
        items = [(str(k), _integer(v or 0)) for k, v in monthly_weights.items()]
    else:
        items = [(f"{i + 1:02d}", _integer(v or 0)) for i, v in enumerate(monthly_weights)]
    items = [(month, weight) for month, weight in items if month in MONTHS]
    if len(items) != 12:
        raise ScenarioError("年度客房收入必须包含12个月权重")
    return dict(largest_remainder(_integer(target_cents), items))


def allocate_annual_room_rev(target_cents, monthly_weights):
    return allocate_annual_room_revenue(target_cents, monthly_weights)


def _metric_row(key, report_code):
    definition = METRICS.get(METRIC_KEYS.get(key, ""), {})
    row = (definition.get("rows") or {}).get(report_code)
    return row or FALLBACK_ROWS.get(report_code, {}).get(key)


def _row_map(report_code):
    result = dict(FALLBACK_ROWS.get(report_code, {}))
    for key in METRIC_KEYS:
        row = _metric_row(key, report_code)
        if row:
            result[key] = row
    result["final"] = FINAL_PROFIT_ROWS.get(report_code, result.get("final"))
    return result


def _metric_label(key):
    definition = METRICS.get(METRIC_KEYS.get(key, ""), {})
    return definition.get("label") or {
        "rooms": "房间数", "sellable": "总可卖房", "sold": "已售房", "occ": "出租率",
        "adr": "平均房价 ADR", "revpar": "RevPAR", "room_rev": "客房收入",
        "total": "酒店总收入", "gop": "经营毛利润", "operating": "酒店经营利润",
        "npi": "NPI", "final": "净利润",
    }.get(key, key)


def _fallback_unit(key):
    if key in {"rooms", "sellable", "sold"}:
        return "COUNT"
    return "RATIO" if key == "occ" else "MONEY"


def _manifest_path(template):
    if not template or not getattr(template, "manifest_path", ""):
        return None
    return resolve_template_path(template.manifest_path)


@lru_cache(maxsize=8)
def _read_manifest(path_text):
    path = Path(path_text)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _manifest_rows(upload):
    path = _manifest_path(getattr(upload, "template", None))
    if not path:
        return {}
    result = {}
    for report_code, report in (_read_manifest(str(path)).get("reports") or {}).items():
        rows = OrderedDict()
        for item in report.get("mapping") or []:
            code = item.get("row_code")
            if code and code not in rows:
                rows[code] = {"label": item.get("row_label") or code, "unit": item.get("unit") or "MONEY"}
        result[report_code] = rows
    return result


def _template_manifest(upload=None):
    path = _manifest_path(getattr(upload, "template", None)) if upload is not None else None
    if path:
        manifest = _read_manifest(str(path))
        if manifest:
            return manifest
    fallback = Path(settings.BASE_DIR) / "artifacts" / "template_manifest.json"
    return _read_manifest(str(fallback))


def _formula_graphs(manifest):
    graphs = {}
    if not manifest:
        return graphs
    for report_code in REPORT_CODES:
        try:
            graphs[report_code] = build_report_graph(manifest, report_code)
        except ValueError:
            continue
    return graphs


def _upload_tables(upload):
    tables = {code: OrderedDict() for code in REPORT_CODES}
    metadata = _manifest_rows(upload)
    values = NormalizedValue.objects.filter(upload=upload).order_by("report_code", "row_code", "id")
    for value in values:
        if value.report_code not in tables:
            continue
        row = tables[value.report_code].setdefault(value.row_code, {
            "label": value.row_label or metadata.get(value.report_code, {}).get(value.row_code, {}).get("label", value.row_code),
            "unit": str(value.unit), "before": OrderedDict(),
        })
        row["before"][_period(value.period)] = _cell(value.value_int, str(value.unit), ratio_num=value.ratio_num, ratio_den=value.ratio_den)
    return tables


def _mapping_tables(baseline):
    if not isinstance(baseline, Mapping):
        raise ScenarioError("场景基线必须是上传版本或报表映射")
    source = baseline if any(str(key) in REPORT_CODES for key in baseline) else {REPORT_CODES[0]: baseline}
    tables = {code: OrderedDict() for code in REPORT_CODES}
    for report_code, report in source.items():
        if report_code not in tables or not isinstance(report, Mapping):
            continue
        for row_code, value in report.items():
            if not isinstance(value, Mapping):
                continue
            before = value.get("before", value)
            row = tables[report_code].setdefault(row_code, {
                "label": value.get("label") or value.get("row_label") or row_code,
                "unit": str(value.get("unit") or "MONEY"), "before": OrderedDict(),
            })
            for period, cell in before.items():
                row["before"][_period(period)] = _cell(cell, row["unit"])
    return tables


def _source_identity(upload):
    template = getattr(upload, "template", None)
    return {
        "baseline_upload_id": str(upload.pk), "baseline_id": str(upload.pk),
        "baseline_sha256": str(getattr(upload, "sha256", "")), "project_id": int(upload.project_id),
        "cycle_id": int(upload.cycle_id), "template_id": int(upload.template_id) if upload.template_id else None,
        "template_version": str(getattr(template, "version", "")), "rule_version": RULE_VERSION,
    }


def _is_admin(actor):
    return actor is None or getattr(actor, "is_superuser", False) or getattr(actor, "role", "") == "ADMIN"


def _require_admin(actor):
    if not _is_admin(actor):
        raise PermissionError("只有管理端可以操作场景")


def _assert_current(upload, *, write=False):
    if str(upload.status) != UploadVersion.Status.APPROVED:
        raise ScenarioError("场景基线必须是已批准版本")
    pc = ProjectCycle.objects.filter(project_id=upload.project_id, cycle_id=upload.cycle_id).first()
    if not pc or pc.current_upload_id != upload.pk:
        raise ScenarioError("场景基线已不是项目当前批准版本")
    cycle = BudgetCycle.objects.get(pk=upload.cycle_id)
    if cycle.status == BudgetCycle.Status.FROZEN:
        raise ScenarioError("预算周期已冻结，场景不可写入")
    if write and not pc.is_open:
        raise ScenarioError("项目周期已关闭，场景不可写入")
    if write and cycle.status not in {BudgetCycle.Status.OPEN, BudgetCycle.Status.ADJUSTING}:
        raise ScenarioError("当前预算周期不允许写入场景")
    return pc, cycle


def _validate_source(tables, *, require_all=False):
    missing = []
    reports = REPORT_CODES if require_all else (REPORT_CODES[0],)
    for report_code in reports:
        rows = tables.get(report_code) or {}
        row_map = _row_map(report_code)
        for key in REQUIRED_KEYS:
            if not row_map.get(key) or row_map[key] not in rows:
                missing.append(f"{report_code}:{key}")
        for key in REQUIRED_KEYS:
            row = rows.get(row_map.get(key))
            periods = (row or {}).get("before", {})
            if any(month not in periods for month in MONTHS):
                missing.append(f"{report_code}:{key}:period")
    if missing:
        raise ScenarioError("基线缺少完整十二个月数据，不能由年度数均摊：" + ",".join(missing))
    for report_code, rows in tables.items():
        for target, dependencies in _profit_rules(report_code).items():
            target_code = f"R{target:04d}"
            codes = [f"R{row:04d}" for row, _ in dependencies]
            if target_code not in rows or any(code not in rows for code in codes):
                continue
            for month in MONTHS:
                actual = (rows[target_code].get("before") or {}).get(month)
                parts = [(rows[code].get("before") or {}).get(month) for code in codes]
                if actual is None or any(part is None for part in parts):
                    raise ScenarioError(f"基线缺少月度来源：{report_code}:{target_code}:{month}")
                expected = sum(_numeric(part) * sign for part, (_, sign) in zip(parts, dependencies))
                if _numeric(actual) != expected:
                    raise ScenarioError(f"基线勾稽不一致：{report_code}:{target_code}:{month}，请先修订工作簿并重传批准")


def _normalise_inputs(inputs, *, driver=None, values=None, annual_room_rev=None, annual_room_rev_delta=None, cost_mode=None):
    if isinstance(inputs, str):
        inputs = {"driver": inputs, "values": values or {}}
    elif inputs is None:
        inputs = {}
    if not isinstance(inputs, Mapping):
        raise ScenarioError("场景输入必须是对象")
    data = dict(inputs)
    driver = driver or data.get("driver")
    if not driver:
        raise ScenarioError("请选择场景驱动")
    driver = str(driver).upper().strip()
    if driver not in DRIVERS:
        raise ScenarioError("未知场景驱动")
    cost_mode = str(cost_mode or data.get("cost_mode") or "fixed").strip().lower()
    if cost_mode not in COST_MODES:
        raise ScenarioError("未知成本联动模式，请选择 fixed 或 proportional")
    if values is not None:
        data["values"] = values
    if annual_room_rev is not None:
        data["annual_room_rev"] = annual_room_rev
    if annual_room_rev_delta is not None:
        data["annual_room_rev_delta"] = annual_room_rev_delta
    monthly = data.get("monthly") or data.get("month_values") or data.get("values") or data.get("months") or {}
    if isinstance(monthly, (list, tuple)):
        monthly = {f"{i + 1:02d}": value for i, value in enumerate(monthly)}
    if not isinstance(monthly, Mapping):
        monthly = {}
    data["driver"] = driver
    data["cost_mode"] = cost_mode
    data["monthly"] = {f"{int(str(month)):02d}": value for month, value in monthly.items() if str(month).isdigit() and 1 <= int(str(month)) <= 12}
    fixed_costs = data.get("fixed_costs") or data.get("costs") or {}
    if not isinstance(fixed_costs, Mapping):
        raise ScenarioError("fixed_costs 参数必须是对象")
    if fixed_costs:
        raise ScenarioError("fixed_costs 暂不支持直接覆盖；请清空该参数并通过 cost_mode 选择成本联动")
    data["fixed_costs"] = {}
    return data


def _baseline_monthly(tables, report_code, key):
    row = (tables.get(report_code) or {}).get(_row_map(report_code).get(key, ""))
    before = (row or {}).get("before", {})
    if any(month not in before for month in MONTHS):
        raise ScenarioError(f"基线缺少完整十二个月数据：{report_code}:{key}，不能由年度数均摊")
    return {month: _numeric(before[month]) for month in MONTHS}


def _month_target(inputs, monthly_weights):
    driver = inputs["driver"]
    monthly = dict(inputs.get("monthly") or {})
    if driver == "ROOM_REV":
        if inputs.get("annual_room_rev_delta") is not None:
            delta = yuan_to_cents(_decimal(inputs["annual_room_rev_delta"]) * 10_000)
            allocation = allocate_annual_room_revenue(delta, monthly_weights)
            return {month: int(monthly_weights[month]) + allocation[month] for month in MONTHS}
        target_cents = inputs.get("annual_target_cents")
        if target_cents is None and inputs.get("annual_room_rev") is not None:
            target_cents = yuan_to_cents(_decimal(inputs["annual_room_rev"]) * 10_000)
        if target_cents is not None:
            return allocate_annual_room_revenue(target_cents, monthly_weights)
    if set(monthly) != set(MONTHS):
        raise ScenarioError("请提供12个月场景输入")
    return monthly


def _period_state(tables, report_code, month, driver, target, year):
    rooms = _integer(_baseline_monthly(tables, report_code, "rooms")[month])
    available = _integer(_baseline_monthly(tables, report_code, "sellable")[month])
    sold = _integer(_baseline_monthly(tables, report_code, "sold")[month])
    if rooms < 0 or available < 0 or not 0 <= sold <= available:
        raise ScenarioError(f"基线房晚超出可售容量：{report_code}:{month}")
    adr = _round_int(_baseline_monthly(tables, report_code, "adr")[month])
    room_rev = _round_int(_baseline_monthly(tables, report_code, "room_rev")[month])
    if room_rev < 0 or adr < 0 or (not sold and room_rev):
        raise ScenarioError(f"基线客房收入与已售房晚不匹配：{report_code}:{month}")
    occ = Decimal(sold) / available if available else Decimal("0")
    target = _decimal(target)
    if driver == "OCC":
        if not Decimal("0") <= target <= Decimal("100"):
            raise ScenarioError("出租率需在0-100之间")
        sold_after = min(available, max(0, _round_int(Decimal(available) * target / 100)))
        rooms_after, available_after, adr_after = rooms, available, adr
        room_rev_after = sold_after * adr_after
    elif driver == "ADR":
        adr_after = yuan_to_cents(target)
        if adr_after <= 0:
            raise ScenarioError("平均房价需大于0")
        rooms_after, available_after, sold_after = rooms, available, sold
        room_rev_after = sold_after * adr_after
    elif driver == "ROOMS":
        rooms_after = _integer(target)
        if rooms_after <= 0:
            raise ScenarioError("房间数需大于0")
        available_after = rooms_after * calendar.monthrange(year, int(month))[1]
        sold_after = min(available_after, max(0, _round_int(Decimal(available_after) * occ)))
        adr_after = adr
        room_rev_after = sold_after * adr_after
    else:
        room_rev_after = _integer(target)
        if room_rev_after < 0:
            raise ScenarioError("客房收入不得为负数")
        rooms_after, available_after, sold_after = rooms, available, sold
        adr_after = _round_int(Decimal(room_rev_after) / sold_after) if sold_after else 0
    if sold_after > available_after:
        raise ScenarioError("已售房不得超过可卖房")
    revpar_after = _round_int(Decimal(room_rev_after) / available_after) if available_after else 0
    return {
        "rooms": rooms, "available": available, "sold": sold, "occ": occ, "adr": adr, "room_rev": room_rev,
        "rooms_after": rooms_after, "available_after": available_after, "sold_after": sold_after,
        "achieved_occ": Decimal(sold_after) / available_after if available_after else Decimal("0"),
        "adr_after": adr_after, "room_rev_after": room_rev_after, "revpar_after": revpar_after,
        "delta_room_rev": room_rev_after - room_rev,
    }


def _proportional_cost_rows(report_code):
    source = (METRICS.get("cost_room_operating_per_room") or {}).get("source") or {}
    component_expression = (source.get("numerator_components") or {}).get(report_code)
    if not component_expression:
        return None
    rows = tuple(code.strip() for code in str(component_expression).split("+") if code.strip())
    if not rows:
        return None
    return rows


def _profit_chain_missing(rows, report_code):
    rules = _profit_rules(report_code)
    row_map = _row_map(report_code)
    targets = _profit_output_targets(report_code)
    missing = []
    visited = set()

    def visit(number):
        if number in visited:
            return
        visited.add(number)
        dependencies = rules.get(number)
        if dependencies is None:
            missing.append(f"R{number:04d}利润规则")
            return
        for dependency, _sign in dependencies:
            dependency_code = f"R{dependency:04d}"
            if dependency in rules:
                visit(dependency)
            elif dependency_code not in rows:
                missing.append(dependency_code)

    for key in ("gop", "operating", "npi"):
        output_code = row_map.get(key)
        target = targets.get(key)
        if not output_code or output_code not in rows:
            missing.append(output_code or f"{key}输出行")
        if target is None:
            missing.append(f"{key}利润规则")
        else:
            visit(target)
    return tuple(dict.fromkeys(missing))


def _validate_proportional_source(tables, report_code):
    rows = tables.get(report_code) or {}
    row_map = _row_map(report_code)
    room_revenue_code = row_map.get("room_rev")
    cost_rows = _proportional_cost_rows(report_code)
    if not cost_rows:
        raise ScenarioError(f"{report_code}缺少可追溯的客房成本映射行，不能使用 proportional 成本联动")
    rule_targets = {f"R{target:04d}" for target in _profit_rules(report_code)}
    derived_cost_rows = tuple(code for code in cost_rows if code in rule_targets)
    if derived_cost_rows:
        joined = ",".join(derived_cost_rows)
        raise ScenarioError(f"{report_code}的客房成本行 {joined} 是注册利润派生行，不能作为 proportional 独立成本源")
    missing = [code for code in (room_revenue_code, *cost_rows) if not code or code not in rows]
    if missing:
        raise ScenarioError(f"{report_code}缺少 proportional 客房收入/成本来源行：{','.join(missing)}")
    for row_code in (room_revenue_code, *cost_rows):
        row = rows[row_code]
        if str(row.get("unit") or "").upper() != "MONEY":
            raise ScenarioError(f"{report_code}:{row_code}不是金额行，不能按预算客房收入率联动")
        before = row.get("before") or {}
        if any(month not in before for month in MONTHS):
            raise ScenarioError(f"{report_code}:{row_code}缺少完整十二个月来源，不能使用 proportional 成本联动")
    for month in MONTHS:
        room_revenue = _numeric(rows[room_revenue_code]["before"][month])
        room_costs = [_numeric(rows[code]["before"][month]) for code in cost_rows]
        if room_revenue == 0 and any(cost != 0 for cost in room_costs):
            raise ScenarioError(f"{report_code}:{month}月客房收入为零但存在客房成本，无法确定预算成本率")
    missing_rules = _profit_chain_missing(rows, report_code)
    if missing_rules:
        raise ScenarioError(f"{report_code}缺少 proportional 所需利润规则或来源：{','.join(missing_rules)}")
    return cost_rows


def _apply_proportional_room_costs(rows, report_code, states, cost_rows):
    for month, state in states.items():
        base_revenue = _decimal(state["room_rev"])
        target_revenue = _decimal(state["room_rev_after"])
        for cost_code in cost_rows:
            base_cell = (rows[cost_code].get("before") or {}).get(month)
            base_cost = _numeric(base_cell)
            if not base_revenue:
                target_cost = Decimal("0")
            else:
                target_cost = (base_cost * target_revenue / base_revenue).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            _put_after(rows, cost_code, month, target_cost, cost_code)


def _row_cell(rows, row_code, period, default_unit="MONEY"):
    row = rows.get(row_code)
    if not row:
        return _number_cell(0, default_unit)
    return row.get("before", {}).get(period) or _number_cell(0, row.get("unit") or default_unit)


def _ensure_row(rows, row_code, key):
    return rows.setdefault(row_code, {"label": _metric_label(key), "unit": _fallback_unit(key), "before": OrderedDict()})


def _put_after(rows, row_code, period, value, key):
    if row_code:
        _ensure_row(rows, row_code, key).setdefault("after", OrderedDict())[period] = _number_cell(value, _ensure_row(rows, row_code, key).get("unit") or _fallback_unit(key))


def _put_ratio(rows, row_code, period, numerator, denominator, key):
    if row_code:
        _ensure_row(rows, row_code, key).setdefault("after", OrderedDict())[period] = _number_cell(0, "RATIO", numerator=numerator, denominator=denominator)


def _changed_month_codes(rows):
    changed = set()
    for row_code, row in rows.items():
        before = row.get("before") or {}
        after = row.get("after") or {}
        if any(_cell_changed(before.get(month), after.get(month)) for month in MONTHS):
            changed.add(row_code)
    return changed


def _formula_value(rows, num_to_code, row_number, month):
    row_code = num_to_code.get(row_number)
    row = rows.get(row_code)
    if not row:
        raise ScenarioError(f"UNVERIFIED公式来源缺失：R{row_number:04d}")
    cell = (row.get("after") or {}).get(month)
    if cell is None:
        cell = (row.get("before") or {}).get(month)
    if cell is None:
        raise ScenarioError(f"UNVERIFIED公式来源缺失：{row_code}:{month}")
    return _numeric(cell)


def _formula_range(rows, num_to_code, c1, r1, c2, r2, month):
    if c1 != c2:
        raise ScenarioError(f"UNVERIFIED公式区间未确认：{c1}{r1}:{c2}{r2}")
    return [_formula_value(rows, num_to_code, row_number, month) for row_number in range(r1, r2 + 1)]


def _apply_template_formulas(rows, report_code, formula_graph=None, upload=None):
    if not formula_graph:
        return set()
    graph, num_to_code = formula_graph
    changed_codes = _changed_month_codes(rows)
    computed = set()
    for row_code in _topo_order(graph):
        metadata = graph[row_code]
        formula = metadata.get("monthly") or ""
        if not formula or metadata.get("kind") != "derived" or row_code not in rows:
            continue
        dependencies = tuple(dep for dep in metadata.get("deps") or () if dep in rows)
        if len(dependencies) != len(metadata.get("deps") or ()):
            continue
        if not dependencies or not any(dep in changed_codes for dep in dependencies):
            continue
        external_refs = CROSS_REF.findall(formula)
        external_values = {}
        if external_refs:
            if upload is None:
                raise ScenarioError(f"UNVERIFIED跨表公式来源：{report_code}:{row_code}")
            try:
                external_values = load_external_values(upload, external_refs)
            except ValueError as exc:
                raise ScenarioError(f"{report_code}:{row_code}：{exc}") from exc
        ast = parse_formula(formula)
        for month in MONTHS:
            value = _eval(
                ast,
                lambda _col, row, month=month: _formula_value(rows, num_to_code, row, month),
                lambda c1, r1, c2, r2, month=month: _formula_range(rows, num_to_code, c1, r1, c2, r2, month),
                lambda ref: external_values[ref],
            )
            _put_after(rows, row_code, month, value, row_code)
        computed.add(row_code)
        changed_codes.add(row_code)
    return computed


def _apply_profit_rules(rows, report_code, formula_graph=None, upload=None):
    rules = _profit_rules(report_code)
    changed_codes = _changed_month_codes(rows)
    computed_codes = set()
    for target, dependencies in rules.items():
        target_code = f"R{target:04d}"
        target_row = rows.get(target_code)
        dependency_codes = [f"R{row:04d}" for row, _ in dependencies]
        if not target_row or str(target_row.get("unit") or "").upper() != "MONEY":
            continue
        if any(code not in rows for code in dependency_codes):
            continue
        if not any(code in changed_codes for code in dependency_codes):
            continue
        for month in MONTHS:
            value = sum(
                _numeric((rows[code].get("after") or {}).get(month) or _number_cell(0, rows[code].get("unit") or "MONEY")) * sign
                for code, (_, sign) in zip(dependency_codes, dependencies)
            )
            _put_after(rows, target_code, month, value, target_code)
        computed_codes.add(target_code)
        changed_codes.add(target_code)

    template_computed = _apply_template_formulas(rows, report_code, formula_graph, upload)
    computed_codes.update(template_computed)
    changed_codes.update(template_computed)

    row_map = _row_map(report_code)
    for key, target in _profit_output_targets(report_code).items():
        output_code = row_map.get(key)
        target_code = f"R{target:04d}"
        output_row = rows.get(output_code)
        target_row = rows.get(target_code)
        if not output_code or output_code == target_code or target_code not in computed_codes:
            continue
        if not output_row or str(output_row.get("unit") or "").upper() != "MONEY":
            continue
        copy_target = str(report_code).startswith("PL_ZZ") and key == "gop"
        for month in MONTHS:
            target_before = _numeric((target_row.get("before") or {}).get(month) or _number_cell(0, target_row.get("unit") or "MONEY"))
            target_after = _numeric((target_row.get("after") or {}).get(month) or _number_cell(0, target_row.get("unit") or "MONEY"))
            output_before = _numeric((output_row.get("before") or {}).get(month) or _number_cell(0, output_row.get("unit") or "MONEY"))
            _put_after(rows, output_code, month, target_after if copy_target else output_before + target_after - target_before, key)
        computed_codes.add(output_code)
        changed_codes.add(output_code)
    return computed_codes, changed_codes


def _apply_profit_fallback(rows, report_code, states):
    row_map = _row_map(report_code)
    changed_codes = _changed_month_codes(rows)
    delta_by_month = {month: state["delta_room_rev"] for month, state in states.items()}
    for key in ("total", "gop", "operating", "npi", "final"):
        row_code = row_map.get(key)
        row = rows.get(row_code)
        if not row or row_code in changed_codes or str(row.get("unit") or "").upper() != "MONEY":
            continue
        for month, delta in delta_by_month.items():
            before = _numeric((row.get("before") or {}).get(month) or _number_cell(0, row.get("unit") or "MONEY"))
            _put_after(rows, row_code, month, before + delta, key)
        changed_codes.add(row_code)


def _recalculate_registered_ratios(rows, report_code, changed_codes):
    for definition in METRICS.values():
        if definition.get("unit") != "RATIO":
            continue
        ratio_code = (definition.get("rows") or {}).get(report_code)
        numerator_code = (definition.get("numerator_rows") or {}).get(report_code)
        denominator_code = (definition.get("denominator_rows") or {}).get(report_code)
        if not ratio_code or not numerator_code or not denominator_code:
            continue
        ratio_row = rows.get(ratio_code)
        if not ratio_row or ratio_code == numerator_code and str(ratio_row.get("unit") or "").upper() != "RATIO":
            continue
        if numerator_code not in rows or denominator_code not in rows:
            continue
        if not ({numerator_code, denominator_code} & changed_codes):
            continue
        for month in MONTHS:
            numerator = _numeric((rows[numerator_code].get("after") or {}).get(month) or _number_cell(0, rows[numerator_code].get("unit") or "MONEY"))
            denominator = _numeric((rows[denominator_code].get("after") or {}).get(month) or _number_cell(0, rows[denominator_code].get("unit") or "MONEY"))
            _put_ratio(rows, ratio_code, month, _round_int(numerator), _round_int(denominator), ratio_code)


def _report_rows(tables, report_code, inputs, year, formula_graph=None, upload=None):
    rows = deepcopy(tables.get(report_code) or {})
    row_map = _row_map(report_code)
    cost_mode = inputs.get("cost_mode", "fixed")
    proportional_cost_rows = _validate_proportional_source(tables, report_code) if cost_mode == "proportional" else ()
    for row_code, row in rows.items():
        before = row.setdefault("before", OrderedDict())
        row["after"] = deepcopy(before)
    targets = _month_target(inputs, _baseline_monthly(tables, REPORT_CODES[0], "room_rev"))
    states = OrderedDict()
    for month in MONTHS:
        state = _period_state(tables, report_code, month, inputs["driver"], targets[month], year)
        states[month] = state
        for key, value in (
            ("room_rev", state["room_rev_after"]), ("adr", state["adr_after"]), ("revpar", state["revpar_after"]),
        ):
            _put_after(rows, row_map[key], month, value, key)
        _put_ratio(rows, row_map["occ"], month, state["sold_after"], state["available_after"], "occ")
        if inputs["driver"] == "ROOMS":
            for key, value in (("rooms", state["rooms_after"]), ("sellable", state["available_after"]), ("sold", state["sold_after"])):
                _put_after(rows, row_map[key], month, value, key)
        elif inputs["driver"] == "OCC":
            _put_after(rows, row_map["sold"], month, state["sold_after"], "sold")
    if cost_mode == "proportional":
        _apply_proportional_room_costs(rows, report_code, states, proportional_cost_rows)
    _apply_profit_rules(rows, report_code, formula_graph=formula_graph, upload=upload)
    if cost_mode == "fixed":
        _apply_profit_fallback(rows, report_code, states)
    _recalculate_registered_ratios(rows, report_code, _changed_month_codes(rows))
    return rows, states


def _annual_after(rows, report_code, states):
    row_map = _row_map(report_code)
    changed = set()
    for row_code, row in rows.items():
        before, after = row.get("before", {}), row.setdefault("after", OrderedDict())
        monthly_after = {m: _numeric(after[m]) for m in MONTHS if m in after}
        monthly_changed = any(_cell_changed(before.get(month), after.get(month)) for month in MONTHS)
        if str(row.get("unit") or "").upper() == "MONEY" and row_code not in {row_map.get("adr"), row_map.get("revpar")} and monthly_after and monthly_changed:
            value = sum(monthly_after.values(), Decimal("0"))
            after["YEAR"] = _number_cell(value, row.get("unit") or "MONEY")
            changed.add(row_code)
        elif row_code == row_map.get("rooms") and monthly_after and monthly_changed:
            after["YEAR"] = _number_cell(sum(monthly_after.values(), Decimal("0")) / 12, row.get("unit") or "COUNT")
            changed.add(row_code)
        elif row_code in {row_map.get("sellable"), row_map.get("sold")} and monthly_after and monthly_changed:
            after["YEAR"] = _number_cell(sum(monthly_after.values(), Decimal("0")), row.get("unit") or "COUNT")
            changed.add(row_code)
        elif row_code == row_map.get("occ") and states:
            sold = sum(s["sold_after"] for s in states.values())
            available = sum(s["available_after"] for s in states.values())
            after["YEAR"] = _number_cell(0, "RATIO", numerator=sold, denominator=available)
            changed.add(row_code)
        elif row_code == row_map.get("adr") and states:
            sold = sum(s["sold_after"] for s in states.values())
            revenue = sum(s["room_rev_after"] for s in states.values())
            after["YEAR"] = _number_cell(_round_int(Decimal(revenue) / sold) if sold else 0, row.get("unit") or "MONEY")
            changed.add(row_code)
        elif row_code == row_map.get("revpar") and states:
            available = sum(s["available_after"] for s in states.values())
            revenue = sum(s["room_rev_after"] for s in states.values())
            after["YEAR"] = _number_cell(_round_int(Decimal(revenue) / available) if available else 0, row.get("unit") or "MONEY")
            changed.add(row_code)
    for definition in METRICS.values():
        if definition.get("unit") != "RATIO":
            continue
        ratio_code = (definition.get("rows") or {}).get(report_code)
        numerator_code = (definition.get("numerator_rows") or {}).get(report_code)
        denominator_code = (definition.get("denominator_rows") or {}).get(report_code)
        if not ratio_code or not numerator_code or not denominator_code:
            continue
        if not ({numerator_code, denominator_code} & changed):
            continue
        ratio_row, numerator_row, denominator_row = rows.get(ratio_code), rows.get(numerator_code), rows.get(denominator_code)
        if not ratio_row or not numerator_row or not denominator_row:
            continue
        if ratio_code == numerator_code and str(ratio_row.get("unit") or "").upper() != "RATIO":
            continue
        numerator_cell = (numerator_row.get("after") or {}).get("YEAR")
        if numerator_cell is None:
            numerator_cell = (numerator_row.get("before") or {}).get("YEAR")
        denominator_cell = (denominator_row.get("after") or {}).get("YEAR")
        if denominator_cell is None:
            denominator_cell = (denominator_row.get("before") or {}).get("YEAR")
        numerator = _numeric(numerator_cell)
        denominator = _numeric(denominator_cell)
        ratio_row.setdefault("after", OrderedDict())["YEAR"] = _number_cell(0, "RATIO", numerator=_round_int(numerator), denominator=_round_int(denominator))
    return rows


def _cell_changed(before, after):
    if not before or not after:
        return bool(before or after)
    if before.get("unit") == "RATIO" or after.get("unit") == "RATIO":
        return (int(before.get("ratio_num") or 0), int(before.get("ratio_den") or 0)) != (int(after.get("ratio_num") or 0), int(after.get("ratio_den") or 0))
    return int(before.get("value_int") or 0) != int(after.get("value_int") or 0)


def _report_result(rows, report_code, states):
    result_rows, changes = [], []
    for row_code, row in rows.items():
        before, after, changed = row.get("before", {}), row.get("after", {}), False
        for period in MONTHS + ("YEAR",):
            if _cell_changed(before.get(period), after.get(period)):
                changed = True
                changes.append({"report_code": report_code, "row_code": row_code, "period": period,
                                "before": deepcopy(before.get(period)), "after": deepcopy(after.get(period))})
        result_rows.append({"row_code": row_code, "code": row_code, "label": row.get("label") or row_code,
                            "unit": row.get("unit") or "MONEY", "before": before, "after": after, "changed": changed})
    monthly = [{"period": month, "room_rev_before_cents": s["room_rev"], "room_rev_after_cents": s["room_rev_after"],
                "delta_room_rev_cents": s["delta_room_rev"], "achievable_occ": str(s["achieved_occ"]),
                "achievable_occ_percent": str(s["achieved_occ"] * 100)} for month, s in states.items()]
    return {"report_code": report_code, "report_name": REPORTS.get(report_code, report_code), "available": True,
            "periods": list(MONTHS) + ["YEAR"], "rows": result_rows, "monthly": monthly,
            "annual": {"room_rev_before_cents": sum(x["room_rev_before_cents"] for x in monthly),
                       "room_rev_after_cents": sum(x["room_rev_after_cents"] for x in monthly),
                       "delta_room_rev_cents": sum(x["delta_room_rev_cents"] for x in monthly)},
            "changed_source_values": changes}, changes


def _calculate_tables(tables, inputs, year, *, formula_graphs=None, upload=None):
    _validate_source(tables)
    _month_target(inputs, _baseline_monthly(tables, REPORT_CODES[0], "room_rev"))
    reports, changes = OrderedDict(), []
    cost_linkage = OrderedDict()
    for report_code in REPORT_CODES:
        if not tables.get(report_code):
            reports[report_code] = {"report_code": report_code, "report_name": REPORTS.get(report_code, report_code),
                                    "available": False, "periods": list(MONTHS) + ["YEAR"], "rows": [], "monthly": [],
                                    "annual": {}, "changed_source_values": []}
            continue
        if inputs.get("cost_mode", "fixed") == "proportional":
            cost_linkage[report_code] = {"room_revenue_row": _row_map(report_code).get("room_rev"),
                                         "cost_rows": list(_validate_proportional_source(tables, report_code))}
        rows, states = _report_rows(
            tables,
            report_code,
            inputs,
            year,
            formula_graph=(formula_graphs or {}).get(report_code),
            upload=upload,
        )
        report_result, report_changes = _report_result(_annual_after(rows, report_code, states), report_code, states)
        reports[report_code] = report_result
        changes.extend(report_changes)
    canonical = reports.get(REPORT_CODES[0], {})
    cost_mode = inputs.get("cost_mode", "fixed")
    if cost_mode == "proportional":
        costs_fixed = False
        costs_fixed_banner = "比例成本联动：按本预算年度原客房收入成本率重算已映射客房成本行；未使用历史成本比例。"
        assumptions = {"cost_mode": cost_mode, "costs": "proportional_to_budget_room_revenue", "profit_linkage": "registered_rules", "formula_evaluator": False}
    else:
        costs_fixed = True
        costs_fixed_banner = "固定成本假设：成本费用绝对额保持基线不变；仅按客房收入差额联动利润，未执行成本重算公式。"
        assumptions = {"cost_mode": cost_mode, "costs": "absolute_fixed", "profit_linkage": "room_revenue_delta_1_to_1", "formula_evaluator": False}
    return {"rule_version": RULE_VERSION, "driver": inputs["driver"], "driver_label": DRIVER_LABELS[inputs["driver"]],
            "cost_mode": cost_mode,
            "inputs": {"driver": inputs["driver"], "cost_mode": cost_mode, "monthly": {m: str(inputs.get("monthly", {}).get(m, "")) for m in MONTHS},
                       "annual_room_rev": str(inputs.get("annual_room_rev")) if inputs.get("annual_room_rev") is not None else None,
                       "annual_room_rev_delta": str(inputs.get("annual_room_rev_delta")) if inputs.get("annual_room_rev_delta") is not None else None,
                       "fixed_costs": {str(k): str(v) for k, v in (inputs.get("fixed_costs") or {}).items()}},
            "reports": reports, "monthly": canonical.get("monthly", []), "annual": canonical.get("annual", {}),
            "changed_source_values": changes, "costs_fixed": costs_fixed,
            "costs_fixed_banner": costs_fixed_banner, "cost_linkage": {"mode": cost_mode, "reports": cost_linkage},
            "assumptions": assumptions}


def calculate_fixed_cost_scenario(baseline, inputs, *, budget_year=None):
    if isinstance(baseline, UploadVersion):
        tables, year = _upload_tables(baseline), int(budget_year or baseline.cycle.budget_year)
        manifest = _template_manifest(baseline)
        upload = baseline
    else:
        tables, year = _mapping_tables(baseline), int(budget_year or 2026)
        manifest = _template_manifest()
        upload = None
    return _calculate_tables(tables, _normalise_inputs(inputs), year, formula_graphs=_formula_graphs(manifest), upload=upload)


def build_scenario_result(baseline, inputs, *, budget_year=None):
    return calculate_fixed_cost_scenario(baseline, inputs, budget_year=budget_year)


def _parse_clone_args(args, baseline, project, cycle, name, created_by):
    for arg in args:
        if isinstance(arg, UploadVersion):
            baseline = baseline or arg
        elif isinstance(arg, Project):
            project = project or arg
        elif isinstance(arg, BudgetCycle):
            cycle = cycle or arg
        elif isinstance(arg, str):
            name = name or arg
        elif arg is not None and hasattr(arg, "role"):
            created_by = created_by or arg
    return baseline, project, cycle, name, created_by


def clone_scenario(*args, baseline=None, project=None, cycle=None, name=None, created_by=None, actor=None):
    baseline, project, cycle, name, created_by = _parse_clone_args(args, baseline, project, cycle, name, created_by or actor)
    _require_admin(created_by)
    if baseline is None:
        if not project or not cycle:
            raise ScenarioError("请选择项目和预算周期")
        pc = ProjectCycle.objects.filter(project=project, cycle=cycle).select_related("current_upload").first()
        baseline = pc.current_upload if pc else None
    if baseline is None:
        raise ScenarioError("项目尚无当前批准版本")
    _assert_current(baseline, write=True)
    if not name or not str(name).strip():
        raise ScenarioError("请输入场景名称")
    if created_by is None:
        raise ScenarioError("缺少创建人")
    source = _source_identity(baseline)
    return BudgetScenario.objects.create(
        baseline=baseline, name=str(name).strip()[:120],
        inputs={"source_identity": source, "baseline_upload_id": source["baseline_upload_id"],
                "baseline_sha256": source["baseline_sha256"], "rule_version": RULE_VERSION,
                "driver": None, "cost_mode": "fixed", "monthly": {}, "fixed_costs": {}}, results={},
        rule_version=RULE_VERSION, status="DRAFT", error="", created_by=created_by)


def create_scenario(*args, **kwargs):
    return clone_scenario(*args, **kwargs)


def _scenario_source_matches(scenario, baseline):
    source = (scenario.inputs or {}).get("source_identity") or {}
    current = _source_identity(baseline)
    for key in ("baseline_upload_id", "baseline_sha256", "project_id", "cycle_id", "template_id", "rule_version"):
        if source.get(key) not in (None, "") and str(source.get(key)) != str(current.get(key)):
            raise ScenarioError("场景基线来源已变化")


def _scenario_value_inputs(inputs, *, driver=None, monthly_values=None, annual_room_rev=None, annual_room_rev_delta=None, annual_target_cents=None, cost_mode=None):
    data = _normalise_inputs(inputs, driver=driver, values=monthly_values, annual_room_rev=annual_room_rev, annual_room_rev_delta=annual_room_rev_delta, cost_mode=cost_mode)
    if annual_target_cents is not None:
        data["annual_target_cents"] = annual_target_cents
    return data


@transaction.atomic
def _calculate_scenario(scenario, inputs=None, actor=None, *, driver=None, monthly_values=None, annual_room_rev=None, annual_room_rev_delta=None, annual_target_cents=None, values=None, cost_mode=None):
    _require_admin(actor)
    scenario = BudgetScenario.objects.select_for_update().select_related("baseline", "baseline__cycle", "baseline__project").get(pk=getattr(scenario, "pk", scenario))
    if scenario.status == "ISSUED":
        raise ScenarioError("已下发场景不可重新测算")
    baseline = scenario.baseline
    _assert_current(baseline, write=True)
    _scenario_source_matches(scenario, baseline)
    if values is not None and monthly_values is None:
        monthly_values = values
    data = _scenario_value_inputs(inputs if inputs is not None else scenario.inputs, driver=driver, monthly_values=monthly_values, annual_room_rev=annual_room_rev, annual_room_rev_delta=annual_room_rev_delta, annual_target_cents=annual_target_cents, cost_mode=cost_mode)
    result = calculate_fixed_cost_scenario(baseline, data, budget_year=baseline.cycle.budget_year)
    source = (scenario.inputs or {}).get("source_identity") or _source_identity(baseline)
    result.update({"source_identity": source, "baseline_upload_id": source["baseline_upload_id"], "status": "READY"})
    saved_inputs = dict(scenario.inputs or {})
    saved_inputs.update({"source_identity": source, "baseline_upload_id": source["baseline_upload_id"], "baseline_sha256": source["baseline_sha256"], "rule_version": RULE_VERSION, "driver": data["driver"], "cost_mode": data["cost_mode"], "uniform_value": str(data.get("uniform_value") or ""), "monthly": {m: str(data["monthly"].get(m, "")) for m in MONTHS}, "fixed_costs": {str(k): str(v) for k, v in (data.get("fixed_costs") or {}).items()}})
    for key in ("annual_room_rev", "annual_room_rev_delta", "annual_target_cents"):
        if key in data:
            saved_inputs[key] = str(data[key])
    scenario.inputs, scenario.results, scenario.rule_version, scenario.status, scenario.error = saved_inputs, result, RULE_VERSION, "READY", ""
    scenario.save(update_fields=["inputs", "results", "rule_version", "status", "error", "updated_at"])
    return scenario


def calculate_scenario(scenario, inputs=None, actor=None, **kwargs):
    _require_admin(actor)
    scenario = BudgetScenario.objects.select_related("baseline").get(pk=getattr(scenario, "pk", scenario))
    _assert_current(scenario.baseline, write=True)
    try:
        return _calculate_scenario(scenario, inputs=inputs, actor=actor, **kwargs)
    except Exception as exc:
        BudgetScenario.objects.filter(
            pk=scenario.pk, baseline__cycle__status__in=[BudgetCycle.Status.OPEN, BudgetCycle.Status.ADJUSTING],
        ).exclude(status="ISSUED").update(
            status="FAILED", error=str(exc), results={},
        )
        raise


def calculate_fixed_cost(*args, **kwargs):
    return calculate_scenario(*args, **kwargs)


def _result_edit_value(cell):
    if not cell:
        return Decimal("0")
    unit = str(cell.get("unit") or "MONEY").upper()
    if unit == "RATIO":
        denominator = int(cell.get("ratio_den") or 0)
        if denominator:
            return Decimal(int(cell.get("ratio_num") or 0)) / Decimal(denominator)
        return Decimal(int(cell.get("value_int") or 0)) / Decimal("10000")
    return int(cell.get("value_int") or 0)


def _result_leaf_edits(scenario, report_code, editable_codes=None):
    results = scenario.results or {}
    report = results.get("reports", {}).get(report_code) or {}
    if not report.get("available"):
        raise ScenarioError(f"{report_code}缺少可下发的场景报表")
    row_map = _row_map(report_code)
    room_revenue_code = row_map.get("room_rev")
    if not room_revenue_code:
        raise ScenarioError(f"{report_code}缺少客房收入映射行")
    rows = {str(row.get("row_code")): row for row in report.get("rows", [])}
    cost_mode = results.get("cost_mode", "fixed")
    cost_rows = _proportional_cost_rows(report_code) if cost_mode == "proportional" else ()
    if editable_codes is None:
        leaf_codes = (room_revenue_code,)
    else:
        changed_codes = {
            str(row.get("row_code"))
            for row in report.get("rows", [])
            if row.get("changed")
        }
        required_codes = set(cost_rows or ())
        required_codes.add(room_revenue_code)
        missing_required = sorted(code for code in required_codes if code not in editable_codes and rows.get(code, {}).get("changed"))
        if missing_required:
            raise ScenarioError(f"{report_code}无法完整下发关联叶子行：{','.join(missing_required)}")
        leaf_codes = tuple(sorted((changed_codes | required_codes) & set(editable_codes)))
    edits = {}
    for row_code in leaf_codes:
        row = rows.get(row_code)
        if not row:
            raise ScenarioError(f"{report_code}缺少需要下发的叶子行：{row_code}")
        before = row.get("before") or {}
        after = row.get("after") or {}
        for month in MONTHS:
            if month not in before or month not in after:
                raise ScenarioError(f"{report_code}:{row_code}缺少{month}月场景目标，不能完整下发")
            baseline_value = _result_edit_value(before[month])
            target_value = _result_edit_value(after[month])
            if target_value != baseline_value:
                edits.setdefault(row_code, {})[month] = target_value
    if not edits:
        raise ScenarioError(f"{report_code}没有可下发的场景叶子变更")
    return edits


def _scenario_value_for_compare(cell):
    if not cell:
        return Decimal("0")
    value = Decimal(str(cell.get("value_int") or 0))
    return value / Decimal("10000") if str(cell.get("unit") or "MONEY").upper() == "RATIO" else value


def _validate_posted_report(result_report, posted_report, report_code):
    posted_rows = {str(item[0]): item for item in (posted_report or {}).get("rows", []) if item}
    missing = []
    mismatched = []
    for result_row in result_report.get("rows", []):
        if not result_row.get("changed"):
            continue
        row_code = str(result_row.get("row_code"))
        posted = posted_rows.get(row_code)
        if not posted:
            missing.append(row_code)
            continue
        expected = _scenario_value_for_compare((result_row.get("after") or {}).get("YEAR"))
        actual = Decimal(str(posted[4])) if len(posted) > 4 and posted[4] is not None else Decimal("0")
        if actual != expected:
            mismatched.append(f"{row_code}: expected {expected}, got {actual}")
    if missing or mismatched:
        details = []
        if missing:
            details.append("缺少目标行 " + ",".join(missing))
        if mismatched:
            details.append("目标值不一致 " + "; ".join(mismatched))
        raise ScenarioError(f"{report_code}场景结果未能完整下发，" + "；".join(details))


@transaction.atomic
def issue_scenario(scenario, actor=None):
    _require_admin(actor)
    scenario = BudgetScenario.objects.select_for_update().select_related(
        "baseline", "baseline__cycle", "baseline__project"
    ).get(pk=getattr(scenario, "pk", scenario))
    if scenario.status == "ISSUED" and scenario.batch_id:
        return scenario.batch
    if scenario.status != "READY":
        raise ScenarioError("只有已测算场景可以下发")
    baseline = scenario.baseline
    _, cycle = _assert_current(baseline, write=True)
    _scenario_source_matches(scenario, baseline)
    tables = _upload_tables(baseline)
    _validate_source(tables, require_all=True)
    results = scenario.results or {}
    cost_mode = results.get("cost_mode", "fixed")
    editable_codes_by_report = None
    if cost_mode == "proportional":
        from budgeting.services.workflow import _project_report
        editable_codes_by_report = {
            report_code: {
                row_code
                for row_code, row in _project_report(baseline.project, cycle, report_code, upload=baseline)[0].items()
                if row.get("kind") == "leaf"
            }
            for report_code in REPORT_CODES
        }
    edits_by_report = {
        report_code: _result_leaf_edits(
            scenario,
            report_code,
            (editable_codes_by_report or {}).get(report_code),
        )
        for report_code in REPORT_CODES
    }
    batch = None
    try:
        from budgeting.services.workflow import create_full_adjustment, issue_adjustment
        for report_code in REPORT_CODES:
            batch = create_full_adjustment(
                cycle,
                baseline.project,
                report_code,
                edits_by_report[report_code],
                f"比例成本场景：{scenario.name}" if cost_mode == "proportional" else f"固定成本场景：{scenario.name}",
                actor=actor,
                due_date=None,
                batch=batch,
                upload=baseline,
            )
            posted_report = ((batch.cascade or {}).get("reports") or {}).get(report_code)
            if cost_mode == "proportional":
                _validate_posted_report(results["reports"][report_code], posted_report, report_code)
    except ScenarioError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        if cost_mode == "proportional":
            raise ScenarioError(f"比例成本场景无法完整下发，已阻断：{exc}") from exc
        raise
    if batch is None:
        raise ScenarioError("场景没有生成调整批次")
    report_deltas = {
        report_code: sum(
            line.allocated_delta_cents
            for line in batch.lines.filter(report_code=report_code)
        )
        for report_code in REPORT_CODES
    }
    room_rev_deltas = {}
    for report_code in REPORT_CODES:
        report_result = results.get("reports", {}).get(report_code) or {}
        result_rows = {str(row.get("row_code")): row for row in report_result.get("rows", [])}
        room_code = _row_map(report_code)["room_rev"]
        room_rev_deltas[report_code] = sum(
            int((result_rows[room_code].get("after") or {}).get(month, {}).get("value_int") or 0)
            - int((result_rows[room_code].get("before") or {}).get(month, {}).get("value_int") or 0)
            for month in MONTHS
        )
    annual = results.get("annual") or {}
    batch.driver = results.get("driver", "ROOM_REV")
    batch.driver_label = results.get("driver_label", "固定成本场景")
    batch.target_room_rev_cents = int(annual.get("room_rev_after_cents") or 0)
    batch.baseline_total_cents = int(annual.get("room_rev_before_cents") or 0)
    canonical_report = REPORT_CODES[0]
    batch.delta_cents = int(report_deltas.get(canonical_report) or 0)
    batch.report_code = canonical_report
    batch.row_code = _row_map(canonical_report)["room_rev"]
    batch.period = "YEAR"
    batch.cascade = {
        "kind": "fixed_cost_scenario",
        "cost_mode": cost_mode,
        "scenario_id": str(scenario.pk),
        "rule_version": RULE_VERSION,
        "source_identity": results.get("source_identity", {}),
        "report_deltas_cents": report_deltas,
        "room_rev_deltas_cents": room_rev_deltas,
        "reports": {
            report_code: {
                "cost_rows": list(_proportional_cost_rows(report_code) or ()),
                "edited_leaf_rows": sorted(edits_by_report[report_code]),
                "changes": (results.get("reports", {}).get(report_code) or {}).get("changed_source_values", []),
                "posted": ((batch.cascade or {}).get("reports") or {}).get(report_code, {}),
            }
            for report_code in REPORT_CODES
        },
    }
    batch.save(update_fields=[
        "driver", "driver_label", "target_room_rev_cents", "baseline_total_cents", "delta_cents",
        "report_code", "row_code", "period", "cascade",
    ])
    issue_adjustment(batch, actor)
    batch.refresh_from_db()
    scenario.batch = batch
    scenario.status = "ISSUED"
    scenario.error = ""
    scenario.save(update_fields=["batch", "status", "error", "updated_at"])
    return batch


def issue_fixed_cost_scenario(*args, **kwargs):
    return issue_scenario(*args, **kwargs)


def scenario_source_identity(scenario):
    return (scenario.inputs or {}).get("source_identity") or _source_identity(scenario.baseline)


__all__ = ["RULE_VERSION", "DRIVERS", "DRIVER_LABELS", "COST_MODES", "COST_MODE_LABELS", "MONTHS", "ScenarioError", "allocate_annual_room_revenue", "allocate_annual_room_rev", "build_scenario_result", "calculate_fixed_cost_scenario", "clone_scenario", "create_scenario", "calculate_scenario", "calculate_fixed_cost", "issue_scenario", "issue_fixed_cost_scenario", "scenario_source_identity"]
