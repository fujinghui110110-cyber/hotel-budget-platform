"""Discover source rows for the wine supplementary metrics.

The selector deliberately only finds a row and its twelve month columns.  It
does not read, coerce, add, or fill any amounts.  Callers can therefore apply
their own value validation and audit rules after the source has been selected.

``discover_wine_sources(workbook)`` accepts an already opened openpyxl
workbook (normally ``read_only=True, data_only=True``) and returns
``(sources, diagnostics)``.  ``sources`` maps ``R9003`` (wine revenue) and
``R9006`` (wine cost), when found, to ``{"sheet", "row", "first_column",
"label"}``.  Diagnostics are dictionaries with ``code``, ``message`` and
``location``; a missing workbook sheet produces at most one metric-level
diagnostic and never twelve cell-level errors.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from numbers import Integral, Real

from openpyxl.utils import get_column_letter


REVENUE = "R9003"
COST = "R9006"
_METRIC_LABELS = {REVENUE: "名酒收入", COST: "名酒成本"}
_MONTHS = tuple(range(1, 13))

# Real source sheets are small but some workbooks advertise 16K columns.  A
# bounded scan keeps discovery predictable while covering the known A:V and
# ordinary inserted-row layouts.
_MAX_SCAN_ROWS = 2000
_MAX_SCAN_COLUMNS = 256


@dataclass(frozen=True)
class _Candidate:
    metric: str
    tier: str
    spec: dict
    location: str


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalise(value) -> str:
    """Normalise presentation punctuation without changing source labels."""

    value = unicodedata.normalize("NFKC", _text(value)).lower()
    return re.sub(r"\s+", "", value)


def _month_number(value):
    """Return a month number for a small set of validated header patterns."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Integral):
        number = int(value)
        return number if 1 <= number <= 12 else None
    if isinstance(value, Real):
        number = float(value)
        if math.isfinite(number) and number.is_integer() and 1 <= number <= 12:
            return int(number)

    text = _normalise(value)
    if not text:
        return None
    # Known templates use 01..12 and 01月..12月.  Accept an optional year
    # prefix as a conservative extension for otherwise identical month rows.
    match = re.fullmatch(
        r"(?:20\d{2}(?:年|[-/.])?)?(0?[1-9]|1[0-2])月?", text
    )
    if not match:
        return None
    return int(match.group(1))


def _month_runs(values):
    runs = []
    if len(values) < len(_MONTHS):
        return runs
    for start in range(0, len(values) - len(_MONTHS) + 1):
        if tuple(_month_number(value) for value in values[start:start + 12]) == _MONTHS:
            runs.append(start + 1)  # openpyxl columns are one-based
    return runs


def _summary_sheet(name):
    compact = _normalise(name)
    return any(token in compact for token in ("损益表", "利润表", "总表", "汇总", "报表"))


def _is_dedicated_sheet(name):
    compact = _normalise(name)
    if compact == _normalise("经营补充指标") or _summary_sheet(name):
        return False
    return "名酒" in compact and ("ood" in compact or "明细" in compact or compact == "名酒")


def _is_supplementary_sheet(name):
    return _normalise(name) == _normalise("经营补充指标")


def _is_ood_label(label):
    compact = _normalise(label)
    return compact.startswith("ood收入") or compact.startswith("ood成本")


def _dedicated_leaf_label(label):
    """Recognise the wine-detail leaf used by the dedicated OOD sheet."""

    compact = _normalise(label)
    if _explicit_wine_label(label):
        return True
    return compact.startswith("ood收入-其他") or compact.startswith("ood成本-其他")


def _orientation(label):
    compact = _normalise(label)
    if compact.startswith("ood收入") or "收入" in compact or "revenue" in compact or "income" in compact:
        return REVENUE
    if compact.startswith("ood成本") or "成本" in compact or "cost" in compact or "expense" in compact:
        return COST
    return None


def _is_leaf_label(label):
    compact = _normalise(label)
    if not compact:
        return False
    # OOD收入/OOD部门成本 are section totals in the original detail sheet;
    # only a labelled suffix is a leaf source.  The same guard excludes
    # obvious aggregate rows in synthetic or revised workbooks.
    if compact in {"ood收入", "ood成本"}:
        return False
    if any(token in compact for token in ("合计", "小计", "总计", "total", "subtotal")):
        return False
    if compact.startswith("ood收入") and compact[len("ood收入"):].startswith("部门"):
        return False
    if compact.startswith("ood成本") and compact[len("ood成本"):].startswith("部门"):
        return False
    return True


def _explicit_wine_label(label):
    compact = _normalise(label)
    return "名酒" in compact or "wine" in compact or "liquor" in compact


def _header_for_row(row_number, month_runs):
    """Pick the nearest preceding validated month header for a source row."""

    preceding = [(row, start) for row, starts in month_runs.items() if row <= row_number for start in starts]
    if preceding:
        header_row, first_column = max(preceding, key=lambda item: item[0])
        return header_row, first_column
    following = [(row, start) for row, starts in month_runs.items() if row > row_number for start in starts]
    if following:
        return min(following, key=lambda item: item[0])
    return None


def _scan_sheet(sheet):
    """Return bounded row values and validated month headers for one sheet."""

    max_row = getattr(sheet, "max_row", None) or _MAX_SCAN_ROWS
    max_column = getattr(sheet, "max_column", None) or _MAX_SCAN_COLUMNS
    max_row = min(max(int(max_row), 1), _MAX_SCAN_ROWS)
    max_column = min(max(int(max_column), 1), _MAX_SCAN_COLUMNS)
    rows = {}
    month_runs = {}
    for row_number, row in enumerate(
        sheet.iter_rows(
            min_row=1,
            max_row=max_row,
            min_col=1,
            max_col=max_column,
            values_only=True,
        ),
        1,
    ):
        values = tuple(row)
        rows[row_number] = values
        starts = _month_runs(values)
        if starts:
            month_runs[row_number] = starts
    return rows, month_runs


def _candidate_for_row(sheet_name, rows, month_runs, row_number, label, label_column, metric, tier):
    header = _header_for_row(row_number, month_runs)
    if header is None:
        return None
    _, first_column = header
    location = f"{sheet_name}!{get_column_letter(label_column)}{row_number}"
    return _Candidate(
        metric=metric,
        tier=tier,
        spec={
            "sheet": sheet_name,
            "row": row_number,
            "first_column": first_column,
            "label": label,
        },
        location=location,
    )


def _sheet_candidates(sheet):
    sheet_name = sheet.title
    rows, month_runs = _scan_sheet(sheet)
    supplementary = _is_supplementary_sheet(sheet_name)
    dedicated = _is_dedicated_sheet(sheet_name)
    summary = _summary_sheet(sheet_name)
    candidates = []
    seen = set()

    for row_number, values in rows.items():
        for label_column, value in enumerate(values, 1):
            label = _text(value)
            if not label:
                continue
            compact = _normalise(label)

            if supplementary:
                # This is the standard analytical fallback, and deliberately
                # only its named wine-revenue row is recognized.
                if compact != _normalise("名酒收入（元）"):
                    continue
                metric = REVENUE
                tier = "supplementary"
            else:
                metric = _orientation(label)
                if metric is None or not _is_leaf_label(label):
                    continue
                if dedicated:
                    # A dedicated wine detail sheet may use OOD收入/成本-
                    # 其他 without repeating 名酒 in every row.
                    if not _dedicated_leaf_label(label):
                        continue
                    tier = "dedicated"
                else:
                    # Summary tables can contain the word 名酒 but are not
                    # source detail.  Generic OOD sheets and explicit wine
                    # labels in other original detail sheets are acceptable.
                    if summary or not _explicit_wine_label(label):
                        continue
                    tier = "original"

            key = (metric, row_number, label)
            if key in seen:
                continue
            seen.add(key)
            candidate = _candidate_for_row(
                sheet_name, rows, month_runs, row_number, label, label_column, metric, tier
            )
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def discover_wine_sources(workbook):
    """Select bounded, source-aware rows for ``R9003`` and ``R9006``.

    A unique candidate with an explicit twelve-month layout in the highest
    available tier is returned.  The row remains a valid source even when
    one or more monthly cells are blank: an identified, non-formula blank is
    a value-reading concern for the caller (the confirmed budget policy reads
    it as zero), not evidence that the source is missing.  Multiple candidates
    at that tier block the metric; values are never summed.  Formula cache
    errors and other cell-value diagnostics remain the caller's responsibility.
    """

    candidates = {REVENUE: [], COST: []}
    for sheet in getattr(workbook, "worksheets", ()):
        for candidate in _sheet_candidates(sheet):
            candidates[candidate.metric].append(candidate)

    sources = {}
    diagnostics = []
    tier_order = {"dedicated": 0, "original": 1, "supplementary": 2}
    for metric in (REVENUE, COST):
        available = candidates[metric]
        if not available:
            diagnostics.append({
                "code": "WINE_SOURCE_MISSING",
                "message": f"未发现{_METRIC_LABELS[metric]}的明确月度来源；未使用零值或推定来源。",
                "location": metric,
            })
            continue
        best_tier = min(tier_order[item.tier] for item in available)
        selected = [item for item in available if tier_order[item.tier] == best_tier]
        if len(selected) != 1:
            locations = ", ".join(item.location for item in selected)
            diagnostics.append({
                "code": "WINE_SOURCE_AMBIGUOUS",
                "message": f"{_METRIC_LABELS[metric]}存在多个同层级明确来源，已阻断且不合并：{locations}。",
                "location": locations,
            })
            continue
        sources[metric] = selected[0].spec
    return sources, diagnostics
