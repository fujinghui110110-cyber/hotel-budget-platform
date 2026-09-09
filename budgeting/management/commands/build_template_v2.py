from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from budgeting.excel.ooxml import formula_manifest
from budgeting.models import REPORTS
from budgeting.services.metrics import METRICS, rolling_years


HISTORY_SHEET = "历史月度输入"
V2_FILE = "平台标准预算模板_V2.xlsx"
V2_MANIFEST = "template_manifest_V2.json"
RELEASE_REPORT = "release_report.json"

_REFERENCE_SHEETS = {
    "T2-市场细分",
    "客房市场说明",
    "客房市场说明 (250928)",
}
_STRUCTURAL_KEYWORDS = (
    "account",
    "code",
    "dept",
    "department",
    "编号",
    "代码",
    "序号",
    "排序",
    "科目",
)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_path(source=None):
    if source:
        return Path(source).expanduser()
    candidate = Path(settings.BASE_DIR) / "artifacts" / "平台标准预算模板_V1.xlsx"
    if candidate.exists():
        return candidate
    return Path(getattr(settings, "SOURCE_WORKBOOK", candidate))


def _golden_path(path=None):
    if path:
        return Path(path).expanduser()
    return Path(settings.BASE_DIR) / "verification" / "v2-prerequisite-golden" / "golden.json"


def _period_columns(budget_year):
    columns = []
    index = 2
    for year, kind in rolling_years(budget_year):
        prefix = {"ACTUAL": "A", "FORECAST": "F", "BUDGET": "B"}[kind]
        for month in range(1, 13):
            columns.append(
                {
                    "column": get_column_letter(index),
                    "period": f"{prefix}{year}M{month:02d}",
                    "year": year,
                    "kind": kind,
                    "month": month,
                }
            )
            index += 1
    return columns


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _structural_columns(sheet):
    columns = set()
    for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 12)):
        for cell in row:
            value = str(cell.value or "").strip().lower()
            if value and any(keyword in value for keyword in _STRUCTURAL_KEYWORDS):
                columns.add(cell.column)
    return columns


def _is_fixed_numeric(sheet, cell, structural_columns):
    if sheet.title == "SYS_META" or sheet.title in _REFERENCE_SHEETS:
        return True
    value = cell.value
    if not _is_number(value):
        return True
    if 1900 <= value <= 2100 or 200000 <= value <= 210000:
        return True
    if sheet.title == "五年铺排" and cell.row in {21, 106}:
        return True
    if sheet.title.startswith("损益表") and cell.row == 5:
        return True
    if cell.column in structural_columns:
        return True
    return False


def _existing_input_cells(workbook):
    result = {}
    for sheet in workbook.worksheets:
        if sheet.title == "SYS_META" or sheet.title in _REFERENCE_SHEETS:
            continue
        structural_columns = _structural_columns(sheet)
        cells = []
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    continue
                if not _is_number(cell.value):
                    continue
                if _is_fixed_numeric(sheet, cell, structural_columns):
                    continue
                cells.append(cell.coordinate)
        if cells:
            result[sheet.title] = sorted(set(cells))
    return result


def _merge_input_cells(*mappings):
    merged = {}
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        for sheet, cells in mapping.items():
            if not isinstance(cells, (list, tuple, set)):
                continue
            merged.setdefault(sheet, set()).update(str(cell) for cell in cells)
    return {sheet: sorted(cells) for sheet, cells in sorted(merged.items())}


def _history_rows():
    rows_by_key = {}

    def add_base(report_code, row_code, metric_code, label, unit, aggregation):
        if not row_code:
            return None
        key = (report_code, row_code)
        existing = rows_by_key.get(key)
        if existing:
            return existing
        item = {
            "report_code": report_code,
            "metric_code": metric_code,
            "row_code": row_code,
            "row_label": label,
            "unit": unit or "MONEY",
            "aggregation": aggregation or "SUM",
            "input": True,
        }
        rows_by_key[key] = item
        return item

    for metric_code, metric in METRICS.items():
        unit = metric.get("unit", "MONEY")
        aggregation = metric.get("aggregation", "SUM")
        for report_code in REPORTS:
            row_code = (metric.get("rows") or {}).get(report_code)
            if row_code and unit != "RATIO" and aggregation != "DERIVED":
                add_base(report_code, row_code, metric_code, metric.get("label", metric_code), unit, aggregation)

            if unit != "RATIO" and aggregation != "DERIVED":
                continue
            numerator = (metric.get("numerator_rows") or {}).get(report_code)
            denominator = (metric.get("denominator_rows") or {}).get(report_code)
            numerator_unit = metric.get("numerator_unit")
            denominator_unit = metric.get("denominator_unit")
            if not numerator_unit:
                numerator_unit = "COUNT" if metric_code == "occ" else "MONEY"
            if not denominator_unit:
                denominator_unit = "COUNT" if metric_code in {"occ", "adr", "revpar"} else "MONEY"
            add_base(
                report_code,
                numerator,
                f"{metric_code}_numerator",
                f"{metric.get('label', metric_code)}（分子）",
                numerator_unit,
                "SUM",
            )
            add_base(
                report_code,
                denominator,
                f"{metric_code}_denominator",
                f"{metric.get('label', metric_code)}（分母）",
                denominator_unit,
                "SUM",
            )

    rows = list(rows_by_key.values())
    for row_number, row in enumerate(rows, start=3):
        row["row_number"] = row_number
    return rows


def _add_history_sheet(workbook, budget_year):
    if HISTORY_SHEET in workbook.sheetnames:
        del workbook[HISTORY_SHEET]
    sheet = workbook.create_sheet(HISTORY_SHEET)
    columns = _period_columns(budget_year)
    rows = _history_rows()
    sheet["A1"] = "历史月度输入（仅填蓝色空白单元格，金额单位：元）"
    sheet["A2"] = "报表 / 指标"
    sheet["A1"].font = Font(bold=True, color="FFFFFF")
    sheet["A1"].fill = PatternFill("solid", fgColor="1F4E78")
    sheet["A1"].alignment = Alignment(horizontal="left")
    sheet["A2"].font = Font(bold=True, color="FFFFFF")
    sheet["A2"].fill = PatternFill("solid", fgColor="5B9BD5")
    for item in columns:
        cell = sheet[f"{item['column']}2"]
        cell.value = item["period"]
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="5B9BD5")
        cell.alignment = Alignment(horizontal="center")
    input_cells = []
    for row in rows:
        row_number = row["row_number"]
        sheet[f"A{row_number}"] = f"{row['report_code']} · {row['row_label']}"
        sheet[f"A{row_number}"].alignment = Alignment(horizontal="left")
        for item in columns:
            cell = sheet[f"{item['column']}{row_number}"]
            if row.get("input") and item["kind"] != "BUDGET":
                cell.fill = PatternFill("solid", fgColor="FFF2CC")
                input_cells.append(cell.coordinate)
            else:
                cell.fill = PatternFill("solid", fgColor="D9E1F2")
    sheet.freeze_panes = "B3"
    sheet.column_dimensions["A"].width = 42
    for item in columns:
        sheet.column_dimensions[item["column"]].width = 13
    return columns, rows, input_cells


def _sheet_manifest(workbook):
    return [
        {
            "name": sheet.title,
            "state": sheet.sheet_state,
            "dimension": sheet.calculate_dimension(),
        }
        for sheet in workbook.worksheets
    ]


def _manifest(base, workbook, columns, rows, input_cells, budget_year, template_path):
    formulas, fingerprint = formula_manifest(template_path)
    history = {
        "sheet": HISTORY_SHEET,
        "header_row": 2,
        "label_column": "A",
        "columns": columns,
        "rows": rows,
    }
    result = dict(base or {})
    result.update(
        {
            "template_version": "V2",
            "budget_year": budget_year,
            "management_v2": True,
            "input_cells": input_cells,
            "history_layout": history,
            "history": history,
            "sheets": _sheet_manifest(workbook),
            "formula_count": len(formulas),
            "formula_manifest_hash": fingerprint,
            "strict_formula_fingerprint": fingerprint,
            "mapped_cell_count": sum(int(item.get("mapped_cell_count", 0)) for item in result.get("reports", {}).values()),
        }
    )
    return result


def _load_base_manifest(source, explicit=None):
    if explicit:
        path = Path(explicit).expanduser()
    else:
        path = Path(source).with_name("template_manifest_V1.json")
        if not path.exists():
            path = Path(settings.BASE_DIR) / "artifacts" / "template_manifest_V1.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CommandError(f"V1 manifest 无法读取：{path}") from exc


def _release_report(output, source, golden):
    golden_data = {}
    if golden.exists():
        try:
            golden_data = json.loads(golden.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            golden_data = {}
    golden_status = str(golden_data.get("status", "MISSING")).upper()
    candidate_sha256 = _sha256(output)
    golden_sha256 = (
        golden_data.get("template_sha256")
        or golden_data.get("candidate_sha256")
        or golden_data.get("artifact_sha256")
    )
    hash_matches = bool(golden_sha256) and golden_sha256 == candidate_sha256
    status = "READY" if golden_status == "PASS" and hash_matches else "BLOCKED"
    blockers = []
    if golden_status != "PASS":
        blockers.append("黄金样本未通过，V2 仅为候选模板，不得正式启用")
    elif not hash_matches:
        blockers.append("黄金样本未绑定当前 V2 候选文件哈希，不得正式启用")
    report = {
        "status": status,
        "golden_status": golden_status,
        "golden_report": str(golden),
        "source_v1": str(source),
        "source_v1_sha256": _sha256(source),
        "candidate_sha256": candidate_sha256,
        "golden_template_sha256": golden_sha256,
        "source_preserved": source.exists(),
        "hash_matches": hash_matches,
        "blockers": blockers,
    }
    return report


class Command(BaseCommand):
    help = "Build a management cockpit V2 candidate from the preserved V1 template."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=None)
        parser.add_argument("--manifest", default=None)
        parser.add_argument("--output-dir", default=None)
        parser.add_argument("--golden-report", default=None)

    def handle(self, *args, **options):
        source = _source_path(options.get("source"))
        if not source.exists():
            raise CommandError(f"V1 模板不存在：{source}")
        output_dir = Path(options.get("output_dir") or (Path(settings.BASE_DIR) / "artifacts" / "v2"))
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / V2_FILE
        manifest_path = output_dir / V2_MANIFEST
        release_path = output_dir / RELEASE_REPORT
        base = _load_base_manifest(source, options.get("manifest"))
        budget_year = int(base.get("budget_year", getattr(settings, "BUDGET_YEAR", 2026)))
        shutil.copy2(source, output)
        workbook = load_workbook(output, keep_links=False)
        if "SYS_META" in workbook.sheetnames:
            workbook["SYS_META"]["A6"] = "project_signature"
        source_inputs = _existing_input_cells(workbook)
        columns, rows, history_input_cells = _add_history_sheet(workbook, budget_year)
        input_cells = _merge_input_cells(
            base.get("input_cells"),
            source_inputs,
            {HISTORY_SHEET: history_input_cells},
        )
        workbook.save(output)
        workbook.close()
        workbook = load_workbook(output, read_only=False, keep_links=False)
        manifest = _manifest(base, workbook, columns, rows, input_cells, budget_year, output)
        workbook.close()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        golden = _golden_path(options.get("golden_report"))
        release = _release_report(output, source, golden)
        release_path.write_text(json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(json.dumps({"template": str(output), "manifest": str(manifest_path), "release": release}, ensure_ascii=False))
