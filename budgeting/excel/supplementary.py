from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from budgeting.models import NormalizedValue, REPORTS


SUPPLEMENTARY_SOURCES = {
    "R9001": ("餐厅收入", "B1餐厅汇总", 24, 11),
    "R9002": ("宴会收入", "B2宴会收入", 24, 11),
    "R9003": ("名酒收入（分析补充）", "经营补充指标", 3, 4),
    "R9005": ("季节性产品收入（月饼亭）", "B16月饼亭", 24, 11),
}


def extract_supplementary_values(upload, workbook_path):
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    pending = []
    for row_code, (label, sheet, row_number, first_column) in SUPPLEMENTARY_SOURCES.items():
        if sheet not in workbook.sheetnames:
            continue
        row = next(workbook[sheet].iter_rows(min_row=row_number, max_row=row_number,
                                           min_col=first_column, max_col=first_column + 11))
        monthly = []
        for month, cell in enumerate(row, 1):
            if cell.value is None or cell.data_type == "e" or isinstance(cell.value, bool):
                monthly.append(None)
                continue
            try:
                number = Decimal(str(cell.value))
                cents = int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if number.is_finite() else None
            except (ValueError, InvalidOperation):
                cents = None
            monthly.append(cents)
            if cents is None:
                continue
            for report in REPORTS:
                pending.append(NormalizedValue(upload=upload, report_code=report, row_code=row_code,
                    row_label=label, period=f"{month:02d}", data_year=upload.cycle.budget_year,
                    data_kind="BUDGET", month=month, unit="MONEY", value_int=cents,
                    source_sheet=sheet, source_cell=f"{get_column_letter(first_column + month - 1)}{row_number}"))
        if all(value is not None for value in monthly):
            for report in REPORTS:
                pending.append(NormalizedValue(upload=upload, report_code=report, row_code=row_code,
                    row_label=label, period="YEAR", data_year=upload.cycle.budget_year,
                    data_kind="BUDGET", month=None, unit="MONEY", value_int=sum(monthly),
                    source_sheet=sheet, source_cell=f"{get_column_letter(first_column)}{row_number}",
                    source_formula="SUM(12 months); analysis subset, do not add to report total"))
    workbook.close()
    for report in REPORTS:
        numbers = (23, 24, 25, 26, 27) if report.startswith("PL_ZZ") else (42, 43)
        codes = {f"R{number:04d}" for number in numbers}
        source = NormalizedValue.objects.filter(upload=upload, report_code=report, row_code__in=codes)
        groups = {}
        for value in source:
            groups.setdefault(value.period, []).append(value)
        for period, values in groups.items():
            if {value.row_code for value in values} != codes:
                continue
            first = values[0]
            pending.append(NormalizedValue(upload=upload, report_code=report, row_code="R9004",
                row_label="客房运营成本合计（含人工）", period=period, data_year=first.data_year,
                data_kind=first.data_kind, month=first.month, unit="MONEY",
                value_int=sum(value.value_int for value in values),
                source_sheet=first.source_sheet, source_cell=first.source_cell,
                source_formula=" + ".join(sorted(codes))))
    NormalizedValue.objects.bulk_create(pending, update_conflicts=True,
        unique_fields=["upload", "report_code", "row_code", "period"],
        update_fields=["row_label", "data_year", "data_kind", "month", "value_int",
                       "source_sheet", "source_cell", "source_formula"])
    return len(pending)
