from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
import json
import re
from pathlib import Path
from typing import Any


_RAW_ROW_CODE = re.compile(r"(?:raw\s*)?r\d{3,}", re.IGNORECASE)
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_ROW_CODE = re.compile(r"r\d{3,}", re.IGNORECASE)


def normalize_label(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u3000", " ").strip()
    text = text.lstrip("'＇").strip()
    return " ".join(text.split())


def labels_from_manifest(manifest: Mapping[str, Any] | None, report_code: str) -> dict[str, str]:
    if not isinstance(manifest, Mapping):
        return {}
    reports = manifest.get("reports")
    if not isinstance(reports, Mapping):
        return {}
    report = reports.get(report_code)
    if not isinstance(report, Mapping):
        return {}
    mapping = report.get("mapping") or report.get("cells") or ()
    if not isinstance(mapping, Iterable) or isinstance(mapping, (str, bytes, Mapping)):
        return {}

    candidates: dict[str, list[str]] = {}
    for item in mapping:
        if not isinstance(item, Mapping):
            continue
        row_code = normalize_label(item.get("row_code")).upper()
        label = normalize_label(item.get("row_label") or item.get("label"))
        if not _ROW_CODE.fullmatch(row_code) or not label:
            continue
        candidates.setdefault(row_code, []).append(label)
    return {code: _best_label(values) for code, values in candidates.items() if _best_label(values)}


def resolve_report_labels(
    report_code: str,
    row_codes: Iterable[str] | None = None,
    *,
    manifest: Mapping[str, Any] | None = None,
    current_labels: Any = None,
    source_labels: Any = None,
    historical_labels: Any = None,
) -> dict[str, str]:
    manifest_labels = labels_from_manifest(manifest, report_code)
    current = _collect_labels(current_labels, report_code, default_current=True)
    source = _collect_labels(source_labels, report_code, default_current=True)
    historical = _collect_labels(
        historical_labels, report_code, default_current=False, include_non_current=True
    )

    merged_current = _merge_candidates(current, source)
    codes = _requested_codes(row_codes)
    if row_codes is None:
        codes.update(manifest_labels)
        codes.update(merged_current)
        codes.update(historical)

    resolved: dict[str, str] = {}
    for row_code in sorted(codes):
        code = normalize_label(row_code).upper()
        if not _ROW_CODE.fullmatch(code):
            continue
        label = _best_label(merged_current.get(code, ()))
        if not label:
            label = _normal_label(manifest_labels.get(code, ""))
        if not label:
            label = _best_label(historical.get(code, ()))
        resolved[code] = label or code
    return resolved


def report_row_labels(
    cycle: Any,
    report_code: str,
    row_codes: Iterable[str] | None,
    current_labels: Any = None,
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    manifest = manifest if manifest is not None else _cycle_manifest(cycle)
    manifest_labels = labels_from_manifest(manifest, report_code)
    codes = _requested_codes(row_codes)
    fallback = resolve_report_labels(
        report_code,
        codes if row_codes is not None else None,
        current_labels=current_labels,
    )
    if row_codes is None:
        codes.update(manifest_labels)
        codes.update(fallback)
    return {
        code: _normal_label(manifest_labels.get(code)) or fallback.get(code, code)
        for code in sorted(codes)
    }


def resolve_row_label(
    report_code: str,
    row_code: str,
    *,
    manifest: Mapping[str, Any] | None = None,
    current_labels: Any = None,
    source_labels: Any = None,
    historical_labels: Any = None,
) -> str:
    code = normalize_label(row_code).upper()
    return resolve_report_labels(
        report_code,
        [code],
        manifest=manifest,
        current_labels=current_labels,
        source_labels=source_labels,
        historical_labels=historical_labels,
    ).get(code, code)


def _requested_codes(row_codes: Iterable[str] | None) -> set[str]:
    if row_codes is None:
        return set()
    if isinstance(row_codes, (str, bytes)):
        row_codes = [row_codes]
    return {
        normalize_label(code).upper()
        for code in row_codes
        if _ROW_CODE.fullmatch(normalize_label(code).upper())
    }


def _collect_labels(
    records: Any,
    report_code: str,
    *,
    default_current: bool,
    include_non_current: bool = False,
) -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = {}
    for code, label, is_current in _iter_records(records, report_code, default_current=default_current):
        if not is_current and not include_non_current:
            continue
        row_code = normalize_label(code).upper()
        if not _ROW_CODE.fullmatch(row_code):
            continue
        cleaned = normalize_label(label)
        if cleaned:
            candidates.setdefault(row_code, []).append(cleaned)
    return candidates


def _iter_records(records: Any, report_code: str, *, default_current: bool):
    if records is None:
        return
    if isinstance(records, Mapping):
        if report_code in records and isinstance(records[report_code], (Mapping, list, tuple)):
            records = records[report_code]
        else:
            for code, label in records.items():
                yield code, label, default_current
            return

    if isinstance(records, (str, bytes)):
        return
    for record in records:
        code: Any = None
        label: Any = None
        is_current = default_current
        if isinstance(record, Mapping):
            record_report = record.get("report_code")
            if record_report and record_report != report_code:
                continue
            code = record.get("row_code") or record.get("code")
            label = record.get("row_label") or record.get("label")
            for key in ("is_current", "current", "upload_is_current", "is_current_upload"):
                if key in record and record[key] is not None:
                    is_current = bool(record[key])
                    break
        elif isinstance(record, (tuple, list)) and len(record) >= 2:
            code, label = record[0], record[1]
            if len(record) >= 3 and isinstance(record[2], bool):
                is_current = record[2]
        else:
            continue
        yield code, label, is_current


def _merge_candidates(*sources: Mapping[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for source in sources:
        for code, values in source.items():
            merged.setdefault(code, []).extend(values)
    return merged


def _best_label(values: Iterable[str]) -> str:
    normal = [_normal_label(value) for value in values]
    normal = [value for value in normal if value]
    if not normal:
        return ""
    return Counter(normal).most_common(1)[0][0]


def _normal_label(value: Any) -> str:
    label = normalize_label(value)
    if not label or _RAW_ROW_CODE.fullmatch(label) or _NUMBER.fullmatch(label):
        return ""
    return label


def _cycle_manifest(cycle: Any) -> Mapping[str, Any] | None:
    try:
        from budgeting.services.workflow import active_template

        template = active_template(cycle)
        path_text = getattr(template, "manifest_path", "")
    except Exception:
        try:
            template = cycle.template
            path_text = getattr(template, "manifest_path", "")
        except Exception:
            return None
    if not path_text:
        return None
    path = Path(path_text)
    candidates = [path] if path.is_absolute() else []
    try:
        from django.conf import settings

        for setting_name in ("BASE_DIR", "BUDGET_STORAGE_ROOT"):
            base = getattr(settings, setting_name, None)
            if base:
                candidates.append(Path(base) / path)
    except Exception:
        pass
    candidates.extend([Path.cwd() / path, Path(__file__).resolve().parents[2] / path])
    for candidate in candidates:
        try:
            if candidate.is_file():
                value = json.loads(candidate.read_text(encoding="utf-8"))
                return value if isinstance(value, Mapping) else None
        except (OSError, TypeError, ValueError):
            continue
    return None
