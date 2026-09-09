"""Read-only OOXML previews for historical hotel budget workbooks.

This module intentionally does not use openpyxl or evaluate formulas. It
reads only workbook XML, worksheet XML, shared strings, and number formats from
an OOXML package. Macro, OLE, connection, and external-link parts are never
opened. Formula values are the cached values present in the source package;
they are never recalculated and missing cached values stay None.

The generated files are a reference/browsing cache, not an approved upload or
an accounting source. load_index and load_sheet are the small API used by the
administrator-only UI.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import posixpath
import re
import time
import zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from openpyxl.styles.numbers import is_date_format
from openpyxl.utils.datetime import from_excel


SCHEMA_VERSION = "workbook-reference.v1"
SUPPORTED_SUFFIXES = {".xlsx", ".xlsm"}
DEFAULT_MAX_ROWS = 200
DEFAULT_MAX_COLS = 200
DEFAULT_MAX_CELLS = 10_000
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNZIPPED_BYTES = 500 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
DEFAULT_BUILTIN_FORMATS = {
    0: "General",
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    9: "0%",
    10: "0.00%",
    11: "0.00E+00",
    12: "# ?/?",
    14: "mm-dd-yy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yy h:mm",
    45: "mm:ss",
    46: "[h]:mm:ss",
    47: "mmss.0",
    49: "@",
}
_CELL_RE = re.compile(r"^\$?([A-Za-z]{1,4})\$?([0-9]+)$")
_CELL_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])(\$?)([A-Za-z]{1,3})(\$?)([0-9]+)"
)
_PROJECTS = {
    "szkl": "深圳凯骊酒店",
    "wnxl": "万宁喜来登",
    "wnfp": "万宁福朋",
}


class WorkbookReferenceError(ValueError):
    pass


class UnsafeWorkbookError(WorkbookReferenceError):
    pass


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attribute(element: ET.Element, name: str, default: str | None = None) -> str | None:
    if name in element.attrib:
        return element.attrib[name]
    for key, value in element.attrib.items():
        if _local_name(key) == name:
            return value
    return default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_zip_entry(name: str) -> str:
    normalized = posixpath.normpath(name)
    if (
        name.startswith("/")
        or "\\" in name
        or normalized == ".."
        or normalized.startswith("../")
    ):
        raise UnsafeWorkbookError(f"OOXML package contains a path traversal: {name}")
    if "\x00" in name:
        raise UnsafeWorkbookError("OOXML package contains a NUL byte in a part name")
    return normalized


def _open_safe_package(path: str | Path) -> tuple[Path, zipfile.ZipFile]:
    source = Path(path).expanduser()
    if source.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise UnsafeWorkbookError("only .xlsx and .xlsm reference workbooks are supported")
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.stat().st_size > MAX_ZIP_BYTES:
        raise UnsafeWorkbookError("OOXML package exceeds the 50 MiB compressed-size limit")
    try:
        package = zipfile.ZipFile(source, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise UnsafeWorkbookError("source is not a valid OOXML ZIP package") from exc
    try:
        names: set[str] = set()
        expanded = 0
        for info in package.infolist():
            normalized = _validate_zip_entry(info.filename)
            if normalized in names:
                raise UnsafeWorkbookError(f"duplicate OOXML part: {info.filename}")
            names.add(normalized)
            expanded += info.file_size
            if expanded > MAX_UNZIPPED_BYTES:
                raise UnsafeWorkbookError("OOXML package exceeds the 500 MiB expanded-size limit")
            if info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
                raise UnsafeWorkbookError(f"OOXML part has an unsafe compression ratio: {info.filename}")
        required = {"[Content_Types].xml", "xl/workbook.xml"}
        if not required.issubset(names):
            raise UnsafeWorkbookError("OOXML package is missing workbook.xml or [Content_Types].xml")
    except Exception:
        package.close()
        raise
    return source, package


def _parse_xml(raw: bytes, part: str) -> ET.Element:
    if b"<!DOCTYPE" in raw.upper():
        raise WorkbookReferenceError(f"DOCTYPE is not allowed in {part}")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise WorkbookReferenceError(f"invalid XML in {part}") from exc


def _part_name(target: str | None, base: str = "xl") -> str | None:
    if not target:
        return None
    target = unquote(target)
    if target.startswith("/"):
        target = target[1:]
    part = posixpath.normpath(posixpath.join(base, target))
    if part == "xl" or part.startswith("../") or not part.startswith("xl/"):
        return None
    return part


def _relationships(package: zipfile.ZipFile, part: str) -> dict[str, str]:
    rel_part = f"{posixpath.dirname(part)}/_rels/{posixpath.basename(part)}.rels"
    if rel_part not in package.namelist():
        return {}
    root = _parse_xml(package.read(rel_part), rel_part)
    result: dict[str, str] = {}
    base = posixpath.dirname(part)
    for relationship in root:
        if _local_name(relationship.tag) != "Relationship":
            continue
        relationship_id = relationship.attrib.get("Id")
        target_mode = relationship.attrib.get("TargetMode", "Internal")
        target = _part_name(relationship.attrib.get("Target"), base)
        if relationship_id and target_mode.lower() != "external" and target:
            result[relationship_id] = target
    return result


def _sheet_entries(package: zipfile.ZipFile) -> list[dict[str, Any]]:
    workbook_part = "xl/workbook.xml"
    workbook = _parse_xml(package.read(workbook_part), workbook_part)
    relationships = _relationships(package, workbook_part)
    entries: list[dict[str, Any]] = []
    sheets = next(
        (child for child in workbook if _local_name(child.tag) == "sheets"),
        None,
    )
    if sheets is None:
        return entries
    for position, sheet in enumerate(sheets, start=1):
        if _local_name(sheet.tag) != "sheet":
            continue
        relationship_id = _attribute(sheet, "id")
        part = relationships.get(relationship_id or "")
        if not part or not part.startswith("xl/worksheets/"):
            # A worksheet relationship pointing outside xl/ is ignored; the
            # preview reader never follows external targets.
            continue
        entries.append(
            {
                "id": f"sheet-{position:03d}",
                "name": sheet.attrib.get("name", f"Sheet{position}"),
                "state": sheet.attrib.get("state", "visible"),
                "visible": sheet.attrib.get("state", "visible") == "visible",
                "part": part,
            }
        )
    return entries


def _read_shared_strings(package: zipfile.ZipFile) -> list[str]:
    part = "xl/sharedStrings.xml"
    if part not in package.namelist():
        return []
    strings: list[str] = []
    stream = package.open(part)
    try:
        for _, element in ET.iterparse(stream, events=("end",)):
            if _local_name(element.tag) != "si":
                continue
            strings.append(
                "".join(
                    child.text or ""
                    for child in element.iter()
                    if _local_name(child.tag) == "t"
                )
            )
            element.clear()
    except ET.ParseError as exc:
        raise WorkbookReferenceError("invalid XML in xl/sharedStrings.xml") from exc
    finally:
        stream.close()
    return strings


def _read_number_formats(package: zipfile.ZipFile) -> list[str]:
    part = "xl/styles.xml"
    if part not in package.namelist():
        return ["General"]
    root = _parse_xml(package.read(part), part)
    custom: dict[int, str] = {}
    cell_xfs: ET.Element | None = None
    for child in root:
        name = _local_name(child.tag)
        if name == "numFmts":
            for fmt in child:
                if _local_name(fmt.tag) == "numFmt":
                    try:
                        custom[int(fmt.attrib.get("numFmtId", "0"))] = fmt.attrib.get(
                            "formatCode", "General"
                        )
                    except ValueError:
                        continue
        elif name == "cellXfs":
            cell_xfs = child
    if cell_xfs is None:
        return ["General"]
    result: list[str] = []
    for xf in cell_xfs:
        try:
            number_format_id = int(xf.attrib.get("numFmtId", "0"))
        except ValueError:
            number_format_id = 0
        result.append(
            custom.get(number_format_id, DEFAULT_BUILTIN_FORMATS.get(number_format_id, "General"))
        )
    return result or ["General"]


def _column_number(column: str) -> int:
    number = 0
    for char in column.upper():
        number = number * 26 + ord(char) - 64
    return number


def _column_letter(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def _coordinate_parts(coordinate: str) -> tuple[int, int] | None:
    match = _CELL_RE.fullmatch(coordinate.strip())
    if not match:
        return None
    return int(match.group(2)), _column_number(match.group(1))


def _translate_shared_formula(formula: str, source: str, target: str) -> str:
    source_parts = _coordinate_parts(source)
    target_parts = _coordinate_parts(target)
    if not source_parts or not target_parts:
        return formula
    row_delta = target_parts[0] - source_parts[0]
    col_delta = target_parts[1] - source_parts[1]

    def replace(match: re.Match[str]) -> str:
        absolute_column, column, absolute_row, row_text = match.groups()
        row = int(row_text)
        column_number = _column_number(column)
        if not absolute_column:
            column_number = max(1, column_number + col_delta)
        if not absolute_row:
            row = max(1, row + row_delta)
        return f"{absolute_column}{_column_letter(column_number)}{absolute_row}{row}"

    return _CELL_REF_RE.sub(replace, formula)


def _parse_number(raw: str | None) -> int | float | str | None:
    if raw is None or raw == "":
        return None
    try:
        decimal = Decimal(raw)
    except (InvalidOperation, ValueError):
        return raw
    if not decimal.is_finite():
        return raw
    if decimal == decimal.to_integral_value():
        return int(decimal)
    value = float(decimal)
    return value if math.isfinite(value) else raw


def _clean_number_format(number_format: str) -> str:
    cleaned = re.sub(
        r"\[(?:black|blue|cyan|green|magenta|red|white|yellow|[<>=][^\]]*)\]",
        "",
        number_format,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"_[^;]", "", cleaned)
    cleaned = re.sub(r"\*.", "", cleaned)
    return cleaned.replace("\\", "")


def _format_date(value: Decimal, number_format: str) -> str:
    try:
        moment = from_excel(float(value))
    except (OverflowError, ValueError):
        return str(value)
    if isinstance(moment, _dt.time):
        return moment.strftime("%H:%M:%S")
    code = number_format.lower()
    has_time = any(token in code for token in ("h", "s"))
    has_seconds = "s" in code
    if has_time:
        return moment.strftime("%Y-%m-%d %H:%M:%S" if has_seconds else "%Y-%m-%d %H:%M")
    return moment.strftime("%Y-%m-%d")


def _format_numeric(value: int | float, raw: str, number_format: str) -> str:
    cleaned = _clean_number_format(number_format or "General")
    if not cleaned or cleaned == "General":
        return raw
    try:
        decimal = Decimal(raw)
    except (InvalidOperation, ValueError):
        decimal = Decimal(str(value))
    if is_date_format(cleaned):
        return _format_date(decimal, cleaned)
    sections = cleaned.split(";")
    if decimal < 0 and len(sections) > 1:
        section = sections[1]
    elif decimal == 0 and len(sections) > 2:
        section = sections[2]
    else:
        section = sections[0]
    if decimal == 0 and '"' in section and not re.search(r"[0#]", section):
        return section.replace('"', "").replace("?", " ").strip()
    percent = "%" in section
    number = abs(decimal) * (100 if percent else 1)
    decimal_match = re.search(r"\.([0#?]+)", section)
    decimals = len(decimal_match.group(1)) if decimal_match else 0
    use_grouping = "," in section.split(".", 1)[0]
    quantizer = Decimal(1).scaleb(-decimals)
    number = number.quantize(quantizer, rounding=ROUND_HALF_UP)
    rendered = format(number, f",.{decimals}f" if use_grouping else f".{decimals}f")
    if decimal < 0 and len(sections) == 1:
        rendered = f"-{rendered}"
    first_placeholder = re.search(r"[0#?]", section)
    last_placeholder = list(re.finditer(r"[0#?]", section))
    if first_placeholder and last_placeholder:
        prefix = section[: first_placeholder.start()].replace("\\", "").replace('"', "")
        suffix = section[last_placeholder[-1].end() :].replace("\\", "").replace('"', "")
        suffix = suffix.replace("%", "")
        rendered = f"{prefix}{rendered}{suffix}"
    if percent:
        rendered += "%"
    return rendered


def _cached_value(
    raw: str | None,
    cell_type: str | None,
    inline_text: str | None,
    shared_strings: Sequence[str],
) -> tuple[Any, str | None]:
    if cell_type == "inlineStr":
        return inline_text if inline_text is not None else "", None
    if raw is None:
        return None, None
    if cell_type == "s":
        try:
            index = int(raw)
            return (shared_strings[index] if 0 <= index < len(shared_strings) else raw), None
        except (ValueError, IndexError):
            return raw, "shared_string_missing"
    if cell_type == "b":
        return raw == "1", None
    if cell_type == "e":
        return raw, raw
    if cell_type == "str":
        return raw, raw if raw.startswith("#") else None
    if cell_type == "d":
        return raw, None
    value = _parse_number(raw)
    if isinstance(value, str) and value.startswith("#"):
        return value, value
    return value, None


def _display_value(
    cached: Any,
    raw: str | None,
    number_format: str,
    error_status: str | None,
) -> str | None:
    if cached is None:
        return None
    if error_status:
        return str(cached)
    if isinstance(cached, bool):
        return "TRUE" if cached else "FALSE"
    if isinstance(cached, (int, float)) and not isinstance(cached, bool):
        return _format_numeric(cached, raw or str(cached), number_format)
    return str(cached)


def _parse_sheet(
    package: zipfile.ZipFile,
    entry: Mapping[str, Any],
    shared_strings: Sequence[str],
    number_formats: Sequence[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    part = str(entry["part"])
    cells: list[dict[str, Any]] = []
    shared_formulas: dict[str, tuple[str, str]] = {}
    max_row = 0
    max_column = 0
    rows: set[int] = set()
    columns: set[int] = set()
    formula_count = 0
    error_count = 0
    stream = package.open(part)
    try:
        for _, element in ET.iterparse(stream, events=("end",)):
            if _local_name(element.tag) != "c":
                continue
            coordinate = element.attrib.get("r")
            coordinate_parts = _coordinate_parts(coordinate or "")
            if not coordinate or not coordinate_parts:
                element.clear()
                continue
            formula_element = next(
                (child for child in element if _local_name(child.tag) == "f"),
                None,
            )
            value_element = next(
                (child for child in element if _local_name(child.tag) == "v"),
                None,
            )
            inline_element = next(
                (child for child in element if _local_name(child.tag) == "is"),
                None,
            )
            has_formula = formula_element is not None
            has_value = value_element is not None or inline_element is not None
            if not has_formula and not has_value:
                element.clear()
                continue
            row, column = coordinate_parts
            raw = value_element.text if value_element is not None else None
            inline_text = (
                "".join(
                    child.text or ""
                    for child in inline_element.iter()
                    if _local_name(child.tag) == "t"
                )
                if inline_element is not None
                else None
            )
            cell_type = element.attrib.get("t")
            cached, error_status = _cached_value(raw, cell_type, inline_text, shared_strings)
            formula_status: str | None = None
            formula_text: str | None = None
            formula_kind: str | None = None
            if formula_element is not None:
                formula_kind = formula_element.attrib.get("t", "explicit")
                formula_text = formula_element.text or ""
                if formula_kind == "shared":
                    shared_index = formula_element.attrib.get("si", "")
                    if formula_text:
                        shared_formulas[shared_index] = (formula_text, coordinate)
                    elif shared_index in shared_formulas:
                        base_formula, base_coordinate = shared_formulas[shared_index]
                        formula_text = _translate_shared_formula(
                            base_formula, base_coordinate, coordinate
                        )
                        formula_status = "shared_translated"
                    else:
                        formula_text = f"[shared:{shared_index}]"
                        formula_status = "shared_unresolved"
                formula_text = f"={formula_text}"
                formula_count += 1
            try:
                style_id = int(element.attrib.get("s", "0") or "0")
            except ValueError:
                style_id = 0
            number_format = (
                number_formats[style_id] if 0 <= style_id < len(number_formats) else "General"
            )
            display = _display_value(cached, raw, number_format, error_status)
            cell = {
                "coordinate": coordinate.upper(),
                "row": row,
                "column": column,
                "display_value": display,
                "original_display_value": display,
                "cached_value": cached,
                "cached_value_raw": raw,
                "cache_status": (
                    "missing"
                    if formula_element is not None and cached is None
                    else "cached"
                    if formula_element is not None
                    else "source"
                ),
                "formula": formula_text,
                "formula_kind": formula_kind,
                "formula_status": formula_status,
                "is_formula": has_formula,
                "error_status": error_status,
                "is_error": bool(error_status),
                "number_format": number_format,
                "style_id": style_id,
                "data_type": cell_type or ("formula" if has_formula else "n"),
            }
            cells.append(cell)
            max_row = max(max_row, row)
            max_column = max(max_column, column)
            rows.add(row)
            columns.add(column)
            if error_status:
                error_count += 1
            element.clear()
    except ET.ParseError as exc:
        raise WorkbookReferenceError(f"invalid XML in {part}") from exc
    finally:
        stream.close()
    cells.sort(key=lambda item: (item["row"], item["column"]))
    summary = {
        "id": entry["id"],
        "name": entry["name"],
        "state": entry["state"],
        "visible": entry["visible"],
        "row_count": max_row,
        "col_count": max_column,
        "column_count": max_column,
        "nonempty_row_count": len(rows),
        "nonempty_col_count": len(columns),
        "cell_count": len(cells),
        "formula_count": formula_count,
        "error_count": error_count,
        "used_range": f"A1:{_column_letter(max_column)}{max_row}" if cells else None,
    }
    return summary, cells


def _workbook_identity(source: Path) -> tuple[str, str]:
    name = source.name
    if "喜来登" in name:
        return "wnxl", _PROJECTS["wnxl"]
    if "福朋" in name:
        return "wnfp", _PROJECTS["wnfp"]
    if "凯骊" in name:
        return "szkl", _PROJECTS["szkl"]
    slug = re.sub(r"[^a-z0-9]+", "-", source.stem.lower()).strip("-") or "workbook"
    return slug[:48], source.stem


def _safety_metadata(package: zipfile.ZipFile, source: Path) -> dict[str, Any]:
    names = package.namelist()
    macro_parts = [
        name
        for name in names
        if name.lower().endswith((".bin", ".vba")) or "vbaproject" in name.lower()
    ]
    external_parts = [
        name
        for name in names
        if name.startswith("xl/externalLinks/")
        or name.startswith("xl/connections")
        or name.startswith("xl/ctrlProps/")
        or name.startswith("xl/activeX/")
        or name.startswith("xl/embeddings/")
    ]
    return {
        "read_only": True,
        "approved": False,
        "vba_executed": False,
        "external_links_followed": False,
        "macros_ignored": bool(macro_parts),
        "external_parts_ignored": bool(external_parts),
        "macro_part_count": len(macro_parts),
        "external_part_count": len(external_parts),
        "source_suffix": source.suffix.lower(),
    }


def read_workbook(
    path: str | Path,
    *,
    workbook_id: str | None = None,
    workbook_name: str | None = None,
    include_cells: bool = True,
) -> dict[str, Any]:
    source, package = _open_safe_package(path)
    started = time.perf_counter()
    try:
        source_sha256 = _sha256(source)
        identity_id, identity_name = _workbook_identity(source)
        workbook_id = workbook_id or identity_id
        workbook_name = workbook_name or identity_name
        shared_strings = _read_shared_strings(package)
        number_formats = _read_number_formats(package)
        calculation: dict[str, Any] = {
            "cached_values_only": True,
            "recalculated": False,
            "warning": "Formula values are source caches; this reader never recalculates them.",
        }
        workbook_root = _parse_xml(package.read("xl/workbook.xml"), "xl/workbook.xml")
        calc_pr = next(
            (child for child in workbook_root if _local_name(child.tag) == "calcPr"),
            None,
        )
        if calc_pr is not None:
            calculation["calc_mode"] = calc_pr.attrib.get("calcMode")
            calculation["full_calc_on_load"] = calc_pr.attrib.get("fullCalcOnLoad")
        sheets: list[dict[str, Any]] = []
        for entry in _sheet_entries(package):
            summary, cells = _parse_sheet(package, entry, shared_strings, number_formats)
            if include_cells:
                summary["cells"] = cells
            sheets.append(summary)
        elapsed = time.perf_counter() - started
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "historical_workbook_reference",
            "read_only": True,
            "approved": False,
            "workbook_id": workbook_id,
            "name": workbook_name,
            "source_name": source.name,
            "source_path": str(source),
            "source_sha256": source_sha256,
            "sourceSHA": source_sha256,
            "extension": source.suffix.lower(),
            "calculation": calculation,
            "safety": _safety_metadata(package, source),
            "sheet_count": len(sheets),
            "sheets": sheets,
            "parse_seconds": round(elapsed, 3),
        }
    finally:
        package.close()


def inspect_workbook(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    return read_workbook(path, **kwargs)


class WorkbookReferenceReader:
    def __init__(
        self,
        path: str | Path,
        *,
        workbook_id: str | None = None,
        workbook_name: str | None = None,
    ):
        self.path = Path(path)
        self.workbook_id = workbook_id
        self.workbook_name = workbook_name

    def read(self, *, include_cells: bool = True) -> dict[str, Any]:
        return read_workbook(
            self.path,
            workbook_id=self.workbook_id,
            workbook_name=self.workbook_name,
            include_cells=include_cells,
        )

    def catalog(self) -> dict[str, Any]:
        return self.read(include_cells=False)


def _json_dump(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def build_reference_cache(
    source_dir: str | Path,
    output_dir: str | Path,
    *,
    sources: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    source_root = Path(source_dir).expanduser().resolve()
    output_root = Path(output_dir).expanduser().resolve()
    if sources is None:
        source_paths = sorted(
            path
            for path in source_root.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        )
    else:
        source_paths = []
        for item in sources:
            path = Path(item)
            if not path.is_absolute():
                path = source_root / path
            path = path.resolve()
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise FileNotFoundError(path)
            source_paths.append(path)
    if not source_paths:
        raise FileNotFoundError(f"no .xlsx/.xlsm sources under {source_root}")
    started = time.perf_counter()
    workbooks: list[dict[str, Any]] = []
    catalog: list[dict[str, Any]] = []
    for source in source_paths:
        workbook = read_workbook(source, include_cells=True)
        workbook_id = str(workbook["workbook_id"])
        summaries: list[dict[str, Any]] = []
        for sheet in workbook["sheets"]:
            sheet_id = str(sheet["id"])
            cells = sheet.pop("cells")
            sheet_cache = {
                "schema_version": SCHEMA_VERSION,
                "kind": "historical_workbook_reference_sheet",
                "read_only": True,
                "approved": False,
                "workbook_id": workbook_id,
                "workbook_name": workbook["name"],
                "source_name": workbook["source_name"],
                "source_sha256": workbook["source_sha256"],
                "sourceSHA": workbook["source_sha256"],
                "calculation": workbook["calculation"],
                "safety": workbook["safety"],
                "sheet": sheet,
                "cells": cells,
            }
            relative_path = Path(workbook_id) / f"{sheet_id}.json"
            _json_dump(output_root / relative_path, sheet_cache)
            summary = dict(sheet)
            summary["json_path"] = relative_path.as_posix()
            summaries.append(summary)
            catalog.append(
                {
                    "workbook_id": workbook_id,
                    "workbook_name": workbook["name"],
                    **summary,
                }
            )
        workbooks.append(
            {
                "id": workbook_id,
                "name": workbook["name"],
                "project_name": workbook["name"],
                "source_name": workbook["source_name"],
                "filename": workbook["source_name"],
                "source_path": str(source.relative_to(source_root)),
                "sha256": workbook["source_sha256"],
                "sourceSHA": workbook["source_sha256"],
                "extension": workbook["extension"],
                "sheet_count": len(summaries),
                "sheets": summaries,
                "calculation": workbook["calculation"],
                "safety": workbook["safety"],
                "parse_seconds": workbook["parse_seconds"],
            }
        )
    elapsed = time.perf_counter() - started
    index = {
        "schema_version": SCHEMA_VERSION,
        "kind": "historical_workbook_reference_index",
        "read_only": True,
        "approved": False,
        "reference_only": True,
        "source_dir": str(source_root),
        "cached_values_only": True,
        "recalculated": False,
        "warning": "Reference previews do not constitute Approved data and are not recalculated.",
        "workbooks": workbooks,
        "catalog": catalog,
        "stats": {
            "workbook_count": len(workbooks),
            "sheet_count": len(catalog),
            "elapsed_seconds": round(elapsed, 3),
        },
    }
    _json_dump(output_root / "index.json", index)
    return index


def _default_reference_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "verification" / "reference-workbooks"


def load_index(reference_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(reference_dir).expanduser() if reference_dir else _default_reference_dir()
    path = root / "index.json"
    with path.open(encoding="utf-8") as handle:
        index = json.load(handle)
    if index.get("kind") != "historical_workbook_reference_index":
        raise WorkbookReferenceError("not a historical workbook reference index")
    return index


def list_workbooks(reference_dir: str | Path | None = None) -> list[dict[str, Any]]:
    try:
        return list(load_index(reference_dir).get("workbooks", []))
    except FileNotFoundError:
        return []


def _find_workbook(index: Mapping[str, Any], workbook_id: str) -> Mapping[str, Any]:
    for workbook in index.get("workbooks", []):
        if workbook.get("id") == workbook_id or workbook.get("name") == workbook_id:
            return workbook
    raise KeyError(f"unknown reference workbook: {workbook_id}")


def _find_sheet(workbook: Mapping[str, Any], sheet_id: str) -> Mapping[str, Any]:
    for sheet in workbook.get("sheets", []):
        if sheet.get("id") == sheet_id or sheet.get("name") == sheet_id:
            return sheet
    raise KeyError(f"unknown reference sheet: {sheet_id}")


def paginate_sheet(
    sheet_data: Mapping[str, Any],
    *,
    start_row: int = 1,
    max_rows: int = DEFAULT_MAX_ROWS,
    start_col: int = 1,
    max_cols: int = DEFAULT_MAX_COLS,
    end_row: int | None = None,
    end_col: int | None = None,
    max_cells: int = DEFAULT_MAX_CELLS,
) -> dict[str, Any]:
    if start_row < 1 or start_col < 1:
        raise ValueError("start_row and start_col must be positive")
    if max_rows < 1 or max_cols < 1 or max_cells < 1:
        raise ValueError("render limits must be positive")
    end_row = end_row if end_row is not None else start_row + max_rows - 1
    end_col = end_col if end_col is not None else start_col + max_cols - 1
    if end_row < start_row or end_col < start_col:
        raise ValueError("range end must not precede range start")
    if end_row - start_row + 1 > max_rows:
        raise ValueError(f"single render is limited to {max_rows} rows")
    if end_col - start_col + 1 > max_cols:
        raise ValueError(f"single render is limited to {max_cols} columns")
    selected = [
        cell
        for cell in sheet_data.get("cells", [])
        if start_row <= int(cell["row"]) <= end_row
        and start_col <= int(cell["column"]) <= end_col
    ]
    truncated = len(selected) > max_cells
    if truncated:
        selected = selected[:max_cells]
    rows: dict[int, list[dict[str, Any]]] = {}
    for cell in selected:
        rows.setdefault(int(cell["row"]), []).append(cell)
    row_items = [{"row": row, "cells": row_cells} for row, row_cells in sorted(rows.items())]
    sheet_summary = sheet_data.get("sheet", sheet_data)
    row_count = int(sheet_summary.get("row_count", 0) or 0)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "historical_workbook_reference_page",
        "read_only": True,
        "approved": False,
        "workbook_id": sheet_data.get("workbook_id"),
        "workbook_name": sheet_data.get("workbook_name"),
        "sheet": sheet_summary,
        "cells": selected,
        "rows": row_items,
        "pagination": {
            "start_row": start_row,
            "end_row": end_row,
            "start_col": start_col,
            "end_col": end_col,
            "returned_cell_count": len(selected),
            "truncated_by_cell_limit": truncated,
            "has_more_rows": end_row < row_count,
            "next_row": end_row + 1 if end_row < row_count else None,
        },
        "calculation": sheet_data.get("calculation", {}),
        "safety": sheet_data.get("safety", {}),
    }


def load_sheet(
    workbook_id: str,
    sheet_id: str,
    *,
    reference_dir: str | Path | None = None,
    start_row: int = 1,
    max_rows: int = DEFAULT_MAX_ROWS,
    start_col: int = 1,
    max_cols: int = DEFAULT_MAX_COLS,
    end_row: int | None = None,
    end_col: int | None = None,
    max_cells: int = DEFAULT_MAX_CELLS,
) -> dict[str, Any]:
    root = Path(reference_dir).expanduser() if reference_dir else _default_reference_dir()
    index = load_index(root)
    workbook = _find_workbook(index, workbook_id)
    sheet = _find_sheet(workbook, sheet_id)
    relative_path = Path(str(sheet["json_path"]))
    cache_path = (root / relative_path).resolve()
    root_resolved = root.resolve()
    if not cache_path.is_relative_to(root_resolved):
        raise WorkbookReferenceError("sheet cache path escapes reference directory")
    with cache_path.open(encoding="utf-8") as handle:
        sheet_data = json.load(handle)
    return paginate_sheet(
        sheet_data,
        start_row=start_row,
        max_rows=max_rows,
        start_col=start_col,
        max_cols=max_cols,
        end_row=end_row,
        end_col=end_col,
        max_cells=max_cells,
    )


load_reference_sheet = load_sheet
load_catalog = load_index


__all__ = [
    "DEFAULT_MAX_CELLS",
    "DEFAULT_MAX_COLS",
    "DEFAULT_MAX_ROWS",
    "SCHEMA_VERSION",
    "WorkbookReferenceError",
    "UnsafeWorkbookError",
    "WorkbookReferenceReader",
    "build_reference_cache",
    "inspect_workbook",
    "load_catalog",
    "load_index",
    "load_reference_sheet",
    "load_sheet",
    "list_workbooks",
    "paginate_sheet",
    "read_workbook",
]
