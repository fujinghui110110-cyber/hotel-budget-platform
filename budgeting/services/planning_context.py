from decimal import Decimal, ROUND_HALF_UP

from budgeting.models import NormalizedValue
from budgeting.services.metrics import METRICS, rolling_years
from budgeting.services.trends import _display_value, _metric_unit_label, _project_metric


SUMMARY_CODES = ("adr", "occ", "revenue_room", "revenue_total", "profit_gop", "profit_npi")


def _number(cell, unit):
    if not cell:
        return None
    if unit == "RATIO" and cell.get("ratio_den") is not None:
        denominator = cell["ratio_den"]
        if not denominator:
            return None
        return int((Decimal(cell.get("ratio_num") or 0) / denominator * 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return cell.get("value_int")


def scenario_planning_context(scenario):
    baseline = scenario.baseline
    year = baseline.cycle.budget_year
    report = "PL_TOTAL_WINE"
    values = list(NormalizedValue.objects.filter(upload=baseline, report_code=report).select_related("upload__cycle"))
    report_result = (scenario.results or {}).get("reports", {}).get(report, {})
    outputs = {row["row_code"]: row for row in report_result.get("rows", [])}
    summary, history = [], []
    kinds = {"ACTUAL": "实际", "FORECAST": "预测", "BUDGET": "原预算"}
    years = [{"year": y, "kind": kind, "label": f"{y} {kinds[kind]}"} for y, kind in rolling_years(year)]
    for code in SUMMARY_CODES:
        spec = METRICS[code]
        unit, aggregation = spec["unit"], spec.get("aggregation")
        cells = []
        for dimension in years:
            item = _project_metric(values, spec, report, (dimension["year"], dimension["kind"], None))
            number = item.get("value_int") if item else None
            cells.append({**dimension, "value": number,
                          "display": _display_value(number, unit, "wan", code, aggregation)})
        output = outputs.get((spec.get("rows") or {}).get(report), {})
        after = _number(output.get("after", {}).get("YEAR"), unit)
        before = cells[-1]["value"]
        delta, growth = "—", "—"
        if before is not None and after is not None:
            difference = after - before
            if unit == "RATIO":
                delta = f"{Decimal(difference) / 100:+.2f} 个百分点"
            else:
                delta = ("+" if difference > 0 else "−" if difference < 0 else "") + _display_value(abs(difference), unit, "wan", code, aggregation)
                growth = f"{Decimal(difference) / abs(before) * 100:+.2f}%" if before else "基期为零"
        row = {"code": code, "label": spec["label"], "unit": _metric_unit_label(spec, "wan", code),
               "before_display": cells[-1]["display"],
               "after_display": _display_value(after, unit, "wan", code, aggregation),
               "delta_display": delta, "change_display": growth}
        summary.append(row)
        history.append({**row, "cells": cells})
    return {"scenario_summary_rows": summary, "scenario_history_rows": history,
            "scenario_history_years": years, "baseline_year": year,
            "demo_mode": any(value.source_cell == "DEMO" for value in values)}
