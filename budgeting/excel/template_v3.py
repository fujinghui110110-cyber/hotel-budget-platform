from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.utils import get_column_letter

from budgeting.excel.ooxml import BLOCKED_PREFIXES, cached_errors, formula_manifest, validate_xlsx_zip
from budgeting.management.commands.build_template_v2 import (
    HISTORY_SHEET,
    _existing_input_cells,
    _load_base_manifest,
    _merge_input_cells,
    _sheet_manifest,
)
from budgeting.models import REPORTS
from budgeting.services.metrics import METRICS


TEMPLATE_VERSION = "V3"
RULE_VERSION = "R3"
SOURCE_YEAR = 2026
SUPPLEMENTARY_SHEET = "经营补充指标"
MONTH_COLUMNS = tuple("KLMNOPQRSTUV")
SUPPLEMENTARY_MONTH_COLUMNS = tuple("DEFGHIJKLMNO")
REPORT_CONTRACTS = {
    "PL_TOTAL_WINE": ("酒店损益总表（含名酒）", "J", tuple("LMNOPQRSTUVW"), {29: (28, 24), 30: (41, 28)}),
    "PL_TOTAL_NOWINE": ("酒店损益总表（不含名酒）", "J", tuple("LMNOPQRSTUVW"), {29: (28, 24), 30: (41, 28)}),
    "PL_ZZ_WINE": ("损益表（含名酒）（拆中智）", "S", tuple("FGHIJKLMNOPQ"), {12: (11, 10), 13: (21, 11)}),
    "PL_ZZ_NOWINE": ("损益表（不含名酒）（拆中智）", "S", tuple("FGHIJKLMNOPQ"), {12: (11, 10), 13: (21, 11)}),
}
B12_REQUIRED_INPUTS = tuple(f"{column}{row}" for row in (98, 99) for column in ("K", "L", "M", "N", "R"))
ZZ_REQUIRED_INPUTS = tuple(f"{column}{row}" for row in (121, 122, 123) for column in tuple("FGHIJKLMNOPQ"))
ZERO_BASELINE_DIRECT_INPUTS = {
    "A21前台": ("V26",),
    "B12中餐厅": ("K39", "L39", "Q39", "S39", "V39"),
    "B3宴会厅": ("K49", "V41"),
    "C3康体中心": ("O112", "P93", "R65", "V76"),
    "C5车队": ("Q47", "R47"),
    "E11行政办": ("L63", "O52"),
    "E12人力资源": ("N59", "O59"),
    "E2信息及通讯": ("N52",),
    "E31市场销售部": tuple(f"{column}45" for column in "LMNOPQRSTUV"),
    "E32公关部": ("M73", "P73", "U63", "V63"),
    "E4工程部": ("Q71", "V26"),
    "E5能耗": ("K25", "L25", "U24"),
    "G1员工餐厅分摊": tuple(f"{column}32" for column in MONTH_COLUMNS),
}
SUPPLEMENTARY_ADJUSTMENTS = (
    (5, "餐饮部印刷与文具调整（元）", "B餐饮部汇总", 68),
    (6, "固定支出杂项调整（元）", "F固定支出", 34),
    (7, "前台旅行社佣金调整（元）", "A21前台", 66),
    (8, "送餐服务费尾差调整（元）", "B14送餐", 31),
)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_manifest_hash(input_cells):
    payload = json.dumps(input_cells, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rolling_history_columns(budget_year):
    columns = []
    index = 2
    for year, kind in ((budget_year - 3, "ACTUAL"), (budget_year - 2, "ACTUAL"), (budget_year - 1, "FORECAST")):
        prefix = "A" if kind == "ACTUAL" else "F"
        for month in range(1, 13):
            columns.append({
                "column": get_column_letter(index),
                "period": f"{prefix}{year}M{month:02d}",
                "year": year,
                "kind": kind,
                "month": month,
            })
            index += 1
    return columns


def history_rows():
    rows = {}
    for metric_code, metric in METRICS.items():
        aggregation = metric.get("aggregation", "SUM")
        unit = metric.get("unit", "MONEY")
        if unit != "RATIO" and aggregation != "DERIVED":
            for report_code, row_code in (metric.get("rows") or {}).items():
                if not row_code or "+" in row_code:
                    continue
                key = (report_code, row_code)
                rows.setdefault(key, {
                    "report_code": report_code,
                    "metric_code": metric_code,
                    "row_code": row_code,
                    "row_label": metric.get("label", metric_code),
                    "unit": unit,
                    "aggregation": aggregation,
                    "input": True,
                })
        if aggregation in {"RATIO", "DERIVED"}:
            if metric_code == "occ":
                component_units = {"numerator": "COUNT", "denominator": "COUNT"}
            elif metric_code in {"adr", "revpar", "cost_room_operating_per_room"}:
                component_units = {"numerator": "MONEY", "denominator": "COUNT"}
            else:
                component_units = {"numerator": "MONEY", "denominator": "MONEY"}
            for suffix, field in (("numerator", "numerator_rows"), ("denominator", "denominator_rows")):
                for report_code, row_code in (metric.get(field) or {}).items():
                    if not row_code or "+" in row_code:
                        continue
                    key = (report_code, row_code)
                    rows.setdefault(key, {
                        "report_code": report_code,
                        "metric_code": f"{metric_code}_{suffix}",
                        "row_code": row_code,
                        "row_label": f"{metric.get('label', metric_code)}（{'分子' if suffix == 'numerator' else '分母'}）",
                        "unit": component_units[suffix],
                        "aggregation": "SUM",
                        "input": True,
                    })
    result = list(rows.values())
    for row_number, row in enumerate(result, start=3):
        row["row_number"] = row_number
    return result


def add_history_sheet(workbook, budget_year):
    if HISTORY_SHEET in workbook.sheetnames:
        del workbook[HISTORY_SHEET]
    sheet = workbook.create_sheet(HISTORY_SHEET)
    columns, rows = rolling_history_columns(budget_year), history_rows()
    sheet["A1"] = f"历史月度输入（{budget_year - 3}—{budget_year - 1}，仅填黄色单元格）"
    sheet["A2"] = "报表 / 指标"
    for cell in (sheet["A1"], sheet["A2"]):
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    input_cells = []
    for item in columns:
        cell = sheet[f"{item['column']}2"]
        cell.value = item["period"]
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="5B9BD5")
        cell.alignment = Alignment(horizontal="center")
    for row in rows:
        unit_label = {"MONEY": "元", "COUNT": "房晚/数量", "RATIO": "%"}.get(row["unit"], row["unit"])
        sheet[f"A{row['row_number']}"] = f"{REPORTS.get(row['report_code'], row['report_code'])} · {row['row_label']}（{unit_label}）"
        for item in columns:
            cell = sheet[f"{item['column']}{row['row_number']}"]
            cell.fill = PatternFill("solid", fgColor="FFF2CC")
            input_cells.append(cell.coordinate)
    sheet.freeze_panes = "B3"
    sheet.column_dimensions["A"].width = 48
    for item in columns:
        sheet.column_dimensions[item["column"]].width = 13
    return columns, rows, input_cells


def add_supplementary_sheet(workbook):
    if SUPPLEMENTARY_SHEET in workbook.sheetnames:
        del workbook[SUPPLEMENTARY_SHEET]
    sheet = workbook.create_sheet(SUPPLEMENTARY_SHEET)
    sheet["A1"] = "经营补充指标"
    sheet["A2"] = "名酒收入与模板清理调整项需独立填报；名酒已含餐饮的不得重复加总"
    sheet["A3"] = "名酒收入（元）"
    sheet["C2"] = "年度"
    sheet["C3"] = "=SUM(D3:O3)"
    for month, column in enumerate(range(4, 16), start=1):
        sheet.cell(row=2, column=column, value=f"{month:02d}月")
        cell = sheet.cell(row=3, column=column)
        cell.fill = PatternFill("solid", fgColor="FFF2CC")
        cell.font = Font(color="0000FF")
    sheet["A4"] = "模板清理调整项（默认留空，按项目实际需要填报）"
    input_cells = [f"{get_column_letter(column)}3" for column in range(4, 16)]
    for row, label, _, _ in SUPPLEMENTARY_ADJUSTMENTS:
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=3, value=f"=SUM(D{row}:O{row})")
        for column in range(4, 16):
            cell = sheet.cell(row=row, column=column)
            cell.fill = PatternFill("solid", fgColor="FFF2CC")
            cell.font = Font(color="0000FF")
            input_cells.append(cell.coordinate)
    sheet["A1"].font = Font(bold=True, color="FFFFFF")
    sheet["A1"].fill = PatternFill("solid", fgColor="1F4E78")
    sheet.column_dimensions["A"].width = 64
    sheet.column_dimensions["C"].width = 15
    for column in range(4, 16):
        sheet.column_dimensions[get_column_letter(column)].width = 12
    return input_cells


def _replace_year(value, budget_year):
    shifts = {SOURCE_YEAR - offset: budget_year - offset for offset in range(4)}
    if isinstance(value, datetime):
        try:
            return value.replace(year=shifts.get(value.year, value.year))
        except ValueError:
            return value
    if isinstance(value, date):
        try:
            return value.replace(year=shifts.get(value.year, value.year))
        except ValueError:
            return value
    if isinstance(value, int) and not isinstance(value, bool) and value in shifts:
        return shifts[value]
    if isinstance(value, str):
        for old, new in shifts.items():
            value = re.sub(rf"(?<!\d){old}(?!\d)", str(new), value)
    return value


def parameterize_year(workbook, budget_year):
    changed = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                new = _replace_year(cell.value, budget_year)
                if new != cell.value:
                    cell.value = new
                    changed.append(f"{sheet.title}!{cell.coordinate}")
    meta = workbook["SYS_META"]
    meta["A6"] = "project_signature"
    values = {meta[f"A{row}"].value: row for row in range(1, meta.max_row + 1)}
    for key, value in (("template_version", f"{TEMPLATE_VERSION}-{budget_year}"), ("budget_year", budget_year), ("rule_version", RULE_VERSION)):
        row = values.get(key)
        if row:
            meta[f"B{row}"] = value
    return changed


def repair_confirmed_findings(workbook):
    required_inputs = {"B12中餐厅": list(B12_REQUIRED_INPUTS)}
    repaired = []
    for ref in B12_REQUIRED_INPUTS:
        workbook["B12中餐厅"][ref] = None
        workbook["B12中餐厅"][ref].fill = PatternFill("solid", fgColor="FFF2CC")
        repaired.append(f"B12中餐厅!{ref}:缺失业务源改为待填输入")
    for sheet_name in ("损益表（含名酒）（拆中智）", "损益表（不含名酒）（拆中智）"):
        required_inputs[sheet_name] = list(ZZ_REQUIRED_INPUTS)
        sheet = workbook[sheet_name]
        for ref in ZZ_REQUIRED_INPUTS:
            sheet[ref] = None
            sheet[ref].fill = PatternFill("solid", fgColor="FFF2CC")
        sheet["S121"] = "=ROUND(SUM(F121:Q121),2)"
        sheet["S122"] = "=ROUND(SUM(F122:Q122),2)"
        sheet["S123"] = "=ROUND(SUM(F123:Q123),2)"
        sheet["S124"] = "=ROUND(S113+S121-SUM(S115:S120,S122)+S123,2)"
        repaired.append(f"{sheet_name}!S124:年度税前利润纳入S123投资收益")

    no_wine_total = workbook["酒店损益总表（不含名酒）"]
    for column in tuple("LMNOPQRSTUVW"):
        ref = f"{column}66"
        no_wine_total[ref] = f"={column}44+{column}56+{column}61+{column}65"
        repaired.append(f"酒店损益总表（不含名酒）!{ref}:恢复酒店经营利润派生公式")

    room_revenue = workbook["A1客房收入(新)"]
    room_revenue["AE38"] = "=IF(AE69=0,0,AE100/AE69)"
    room_revenue["X43"] = "=IF(X74=0,0,X105/X74)"
    repaired.extend(
        [
            "A1客房收入(新)!AE38:同比率按实际分母判零",
            "A1客房收入(新)!X43:同比率按实际分母判零",
        ]
    )

    for sheet_name, refs in ZERO_BASELINE_DIRECT_INPUTS.items():
        required_inputs.setdefault(sheet_name, []).extend(refs)
        sheet = workbook[sheet_name]
        for ref in refs:
            sheet[ref] = None
            sheet[ref].fill = PatternFill("solid", fgColor="FFF2CC")
            repaired.append(f"{sheet_name}!{ref}:项目金额残留改为待填输入")

    for month_index, source_column in enumerate(MONTH_COLUMNS):
        supplement_column = SUPPLEMENTARY_MONTH_COLUMNS[month_index]
        workbook["B餐饮部汇总"][f"{source_column}68"] = (
            f"=ROUND((B1餐厅汇总!{source_column}68+B3宴会厅!{source_column}68),2)"
            f"+'{SUPPLEMENTARY_SHEET}'!{supplement_column}5"
        )
        wage_column = get_column_letter(month_index + 2)
        workbook["F固定支出"][f"{source_column}34"] = (
            f"=(+工资福利费!{wage_column}129)+'{SUPPLEMENTARY_SHEET}'!{supplement_column}6"
        )
        workbook["A21前台"][f"{source_column}66"] = (
            f"=ROUND('A1客房收入(新)'!{source_column}99*A21前台!$W$66,2)"
            f"+'{SUPPLEMENTARY_SHEET}'!{supplement_column}7"
        )
        workbook["B14送餐"][f"{source_column}31"] = (
            f"=ROUND({source_column}25*$W$31,2)+'{SUPPLEMENTARY_SHEET}'!{supplement_column}8"
        )
    repaired.extend(
        f"{sheet_name}!{MONTH_COLUMNS[0]}{source_row}:{MONTH_COLUMNS[-1]}{source_row}:"
        "硬编码月度调整改为经营补充指标显式输入"
        for _, _, sheet_name, source_row in SUPPLEMENTARY_ADJUSTMENTS
    )
    return required_inputs, repaired


def clear_stale_inputs(workbook, input_cells):
    cleared = []
    for sheet_name, refs in input_cells.items():
        sheet = workbook[sheet_name]
        for ref in refs:
            cell = sheet[ref]
            if cell.data_type != "f" and cell.value not in (None, ""):
                cell.value = None
                cleared.append(f"{sheet_name}!{ref}")
    return cleared


def recover_dense_row_numeric_inputs(workbook, input_cells):
    recovered = {}
    for sheet_name, refs in input_cells.items():
        if sheet_name not in workbook.sheetnames:
            continue
        sheet = workbook[sheet_name]
        rows = {}
        for ref in refs:
            cell = sheet[ref]
            rows.setdefault(cell.row, []).append(cell.column)
        for row, columns in rows.items():
            span = max(columns) - min(columns) + 1
            if len(columns) < 8 or span > 14:
                continue
            known = set(columns)
            for column in range(min(columns), max(columns) + 1):
                cell = sheet.cell(row=row, column=column)
                if column in known or cell.data_type == "f":
                    continue
                if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                    recovered.setdefault(sheet_name, []).append(cell.coordinate)
    return {sheet: sorted(set(refs)) for sheet, refs in recovered.items()}


def close_report_mapping_inputs(workbook, reports):
    mapped_inputs = {}
    for report in (reports or {}).values():
        sheet_name = report.get("sheet")
        if sheet_name not in workbook.sheetnames:
            continue
        sheet = workbook[sheet_name]
        for item in report.get("mapping") or []:
            ref = item.get("cell")
            if not ref:
                continue
            cell = sheet[ref]
            if cell.data_type == "f" or (isinstance(cell.value, str) and cell.value.startswith("=")):
                continue
            mapped_inputs.setdefault(sheet_name, []).append(ref)
            item["classification"] = "input"
            item["formula"] = None
            item["shared_attributes"] = {}
            item["allowed_functions"] = []
    return {sheet: sorted(set(refs)) for sheet, refs in mapped_inputs.items()}


def normalize_blank_template_formulas(workbook):
    denominator_pattern = re.compile(r"/(\s*(?:(?:'[^']+'|[A-Za-z0-9_\u4e00-\u9fff ]+)!)?\$?[A-Z]{1,3}\$?\d+)")
    denominator_sum_pattern = re.compile(r"/(\s*SUM\([^)]*\))", re.I)
    average_pattern = re.compile(r"AVERAGE\(([^()]*)\)", re.I)
    guarded = []
    blanked = []
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                value = cell.value
                if value == "=":
                    cell.value = None
                    blanked.append(f"{sheet.title}!{cell.coordinate}")
                    continue
                if not isinstance(value, str) or not value.startswith("="):
                    continue
                upper = value.upper()
                if "IFERROR(" in upper or re.search(r"IF\s*\([^,]*(?:=|<>|<=)\s*0", upper):
                    continue
                averages = [match.group(1).strip() for match in average_pattern.finditer(value)]
                if averages:
                    condition = averages[0] if len(averages) == 1 else None
                    count_check = f"COUNT({condition})=0" if condition else "OR(" + ",".join(f"COUNT({item})=0" for item in averages) + ")"
                    cell.value = f"=IF({count_check},0,{value[1:]})"
                    guarded.append(f"{sheet.title}!{cell.coordinate}")
                    continue
                if "/" not in value:
                    continue
                denominators = []
                for match in denominator_pattern.finditer(value):
                    denominator = match.group(1).strip()
                    if denominator not in denominators:
                        denominators.append(denominator)
                for match in denominator_sum_pattern.finditer(value):
                    denominator = match.group(1).strip()
                    if denominator not in denominators:
                        denominators.append(denominator)
                if not denominators:
                    continue
                condition = denominators[0] + "=0" if len(denominators) == 1 else "OR(" + ",".join(f"{item}=0" for item in denominators) + ")"
                cell.value = f"=IF({condition},0,{value[1:]})"
                guarded.append(f"{sheet.title}!{cell.coordinate}")
    return guarded, blanked


def protect_workbook(workbook, input_cells):
    for sheet in workbook.worksheets:
        for ref in input_cells.get(sheet.title, []):
            sheet[ref].protection = Protection(locked=False)
        sheet.protection.sheet = True
        sheet.protection.objects = True
        sheet.protection.scenarios = True
        sheet.protection.selectLockedCells = True
        sheet.protection.selectUnlockedCells = False


def scan_package(path):
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        blocked = [name for name in names if name.startswith(BLOCKED_PREFIXES)]
        external_rels = []
        for name in names:
            if not name.endswith(".rels"):
                continue
            text = zf.read(name).decode("utf-8", errors="ignore")
            if 'TargetMode="External"' in text:
                external_rels.append(name)
        workbook_xml = zf.read("xl/workbook.xml").decode("utf-8", errors="ignore")
    formulas, _ = formula_manifest(path)
    proprietary = [item for item in formulas if any(token in item["formula"].upper() for token in ("DBS(", "VIEW(", "SUBNM("))]
    return {
        "blocked_parts": blocked,
        "external_relationships": external_rels,
        "invalid_defined_names": "#REF!" in workbook_xml,
        "proprietary_formulas": proprietary,
        "cached_errors": cached_errors(path),
        "zip_issues": validate_xlsx_zip(path),
    }


def recalc_libreoffice(source, output_dir, soffice="/opt/homebrew/bin/soffice"):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="budget-v3-lo-profile-"))
    command = [soffice, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "xlsx", "--outdir", str(output_dir), str(source)]
    run = subprocess.run(command, capture_output=True, text=True, timeout=300)
    target = output_dir / Path(source).name
    return target, {"command": command, "returncode": run.returncode, "stdout": run.stdout.strip(), "stderr": run.stderr.strip(), "output_exists": target.exists()}


def validate_management_contract(path):
    workbook = load_workbook(path, read_only=False, data_only=False, keep_links=False)
    checks = []
    for report_code, (sheet_name, annual_col, months, ratios) in REPORT_CONTRACTS.items():
        sheet = workbook[sheet_name]
        annual_rows = range(23, 108) if annual_col == "J" else range(9, 145)
        for row in annual_rows:
            formula = sheet[f"{annual_col}{row}"].value
            populated_months = any(sheet[f"{column}{row}"].value not in (None, "") for column in months)
            if populated_months and row not in ({47, 58, 66, 91, 104, 132, 137} if annual_col == "S" else set()):
                checks.append({"check": "annual_formula", "report": report_code, "cell": f"{annual_col}{row}", "pass": isinstance(formula, str) and formula.startswith("=")})
        for row, (numerator, denominator) in ratios.items():
            formula = str(sheet[f"{annual_col}{row}"].value or "").replace("$", "").upper()
            checks.append({"check": "occ_or_adr", "report": report_code, "cell": f"{annual_col}{row}", "pass": f"{annual_col}{numerator}" in formula and f"{annual_col}{denominator}" in formula})
        if annual_col == "S":
            tax_formula = str(sheet["S124"].value or "").replace("$", "").upper()
            checks.append({"check": "profit_before_tax", "report": report_code, "cell": "S124", "pass": "S113" in tax_formula and "S123" in tax_formula and "S115:S120" in tax_formula and "S122" in tax_formula})
        profit_rows = (33, 90, 105) if annual_col == "J" else (16, 113, 129)
        for row in profit_rows:
            formula = str(sheet[f"{annual_col}{row}"].value or "").upper()
            checks.append({"check": "profit_annual", "report": report_code, "cell": f"{annual_col}{row}", "pass": "SUM(" in formula})
    workbook.close()
    return {"status": "PASS" if all(item["pass"] for item in checks) else "FAIL", "checks": checks, "failed": [item for item in checks if not item["pass"]]}


def build_v3(clean_base, original_source, base_manifest_path, output, manifest_path, report_path, budget_year):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(clean_base, output)
    base = _load_base_manifest(clean_base, base_manifest_path)
    workbook = load_workbook(output, keep_links=False)
    source_inputs = _existing_input_cells(workbook)
    recovered_inputs = recover_dense_row_numeric_inputs(workbook, source_inputs)
    year_changes = parameterize_year(workbook, budget_year)
    required_inputs, repaired = repair_confirmed_findings(workbook)
    columns, rows, history_inputs = add_history_sheet(workbook, budget_year)
    supplementary_inputs = add_supplementary_sheet(workbook)
    denominator_guards, blank_formulas = normalize_blank_template_formulas(workbook)
    mapped_inputs = close_report_mapping_inputs(workbook, base.get("reports"))
    input_cells = _merge_input_cells(
        source_inputs,
        recovered_inputs,
        mapped_inputs,
        required_inputs,
        {HISTORY_SHEET: history_inputs},
        {SUPPLEMENTARY_SHEET: supplementary_inputs},
    )
    cleared = clear_stale_inputs(workbook, input_cells)
    protect_workbook(workbook, input_cells)
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    workbook.calculation.calcMode = "auto"
    workbook.save(output)
    workbook.close()
    formulas, fingerprint = formula_manifest(output)
    history = {"sheet": HISTORY_SHEET, "header_row": 2, "label_column": "A", "columns": columns, "rows": rows}
    manifest = dict(base)
    manifest.update({
        "template_version": f"{TEMPLATE_VERSION}-{budget_year}",
        "budget_year": budget_year,
        "year": budget_year,
        "rule_version": RULE_VERSION,
        "management_v2": True,
        "management_v3": True,
        "input_cells": input_cells,
        "input_manifest_hash": input_manifest_hash(input_cells),
        "history": history,
        "history_layout": history,
        "history_month_count": len(columns),
        "supplementary": {
            "sheet": SUPPLEMENTARY_SHEET,
            "row_code": "R9003",
            "annual_cell": "C3",
            "month_cells": supplementary_inputs,
            "included_in_pnl_total": False,
        },
        "formula_count": len(formulas),
        "formula_manifest_hash": fingerprint,
        "strict_formula_fingerprint": fingerprint,
        "source_identity": {"path": str(original_source), "sha256": sha256_file(original_source), "business_sheet_count": 67},
        "clean_base": {"path": str(clean_base), "sha256": sha256_file(clean_base)},
        "required_business_inputs": required_inputs,
        "workbook_sha256": sha256_file(output),
    })
    check_wb = load_workbook(output, read_only=True, keep_links=False)
    manifest["sheets"] = _sheet_manifest(check_wb)
    check_wb.close()
    Path(manifest_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    package = scan_package(output)
    contract = validate_management_contract(output)
    blockers = []
    if any((package["blocked_parts"], package["external_relationships"], package["invalid_defined_names"], package["proprietary_formulas"], package["zip_issues"])):
        blockers.append("OOXML 清理检查未通过")
    if contract["status"] != "PASS":
        blockers.append("四张管理报表公式契约未通过")
    report = {
        "status": "LOCAL_TRIAL" if not blockers else "BLOCKED",
        "publication_status": "BLOCKED",
        "publication_blockers": blockers + [
            "B12中餐厅第98至99行的原始业务来源缺失，填报前必须补齐黄色输入格",
            "含名酒拆中智表的375处不含名酒跨口径引用需业务确认",
            "尚未完成 Microsoft Excel 黄金样本逐单元格对账",
        ],
        "budget_year": budget_year,
        "source_sha256_unchanged": sha256_file(original_source) == manifest["source_identity"]["sha256"],
        "year_changes": len(year_changes),
        "stale_input_cells_cleared": len(cleared),
        "confirmed_formula_repairs": repaired,
        "explicit_zero_denominator_guards": len(denominator_guards),
        "empty_formula_nodes_blanked": len(blank_formulas),
        "business_confirmation_required": [
            "B12中餐厅第98至99行的原始业务来源缺失，填报前必须补齐黄色输入格",
            "含名酒拆中智表的375处不含名酒跨口径引用需业务确认",
        ],
        "history_month_count": len(columns),
        "input_cell_count": sum(len(value) for value in input_cells.values()),
        "package_scan": package,
        "management_contract": contract,
        "template": str(output),
        "manifest": str(manifest_path),
    }
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest, report
