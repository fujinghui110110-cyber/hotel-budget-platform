import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

from django.conf import settings
from budgeting.services.template_paths import resolve_template_path
from django.core import signing
from openpyxl.formula.translate import Translator, TranslatorError
from openpyxl.formula.tokenizer import Tokenizer, TokenizerError

PROPRIETARY_FUNCS = ("DBS(", "VIEW(", "SUBNM(")
BLOCKED_PREFIXES = (
    "xl/externalLinks/",
    "xl/connections",
    "xl/ctrlProps/",
    "xl/embeddings/",
    "xl/activeX/",
)
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNZIPPED_BYTES = 500 * 1024 * 1024
MAX_ZIP_ENTRIES = 10_000
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_RATIO = 100
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_MAIN_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
XML_DTD_RE = re.compile(r"<\s*!\s*(?:DOCTYPE|ENTITY)\b", re.I)
RELATIONSHIP_RE = re.compile(r"<\s*Relationship\b(?P<attrs>[^>]*)>", re.I | re.S)
XML_ATTR_RE = re.compile(r"([\w:.-]+)\s*=\s*([\"'])(.*?)\2", re.S)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalised_zip_name(name):
    return posixpath.normpath(name.replace("\\", "/"))


def _unsafe_zip_name(name):
    normalized = _normalised_zip_name(name)
    # ZIP directory records end in one slash; interior empty components remain unsafe.
    parts = (name[:-1] if name.endswith("/") else name).split("/")
    return (
        not name
        or name.startswith(("/", "\\"))
        or "\\" in name
        or normalized.startswith("../")
        or normalized in {"..", "."}
        or any(part in {"", ".", ".."} for part in parts)
        or any(re.match(r"^[A-Za-z]:", part) for part in parts)
    )


def _decode_xml_for_scan(raw):
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            text = raw.decode(encoding)
        except UnicodeError:
            continue
        if "<" in text:
            return text
    return raw.decode("utf-8", errors="ignore")


def _external_relationship_issues(filename, text):
    issues = []
    for match in RELATIONSHIP_RE.finditer(text):
        attrs = {
            key.lower(): value
            for key, _, value in XML_ATTR_RE.findall(match.group("attrs"))
        }
        if attrs.get("targetmode", "").strip().lower() != "external":
            continue
        rel_type = attrs.get("type", "").lower()
        if rel_type.endswith("/hyperlink"):
            issues.append(("P0", "EXTERNAL_HYPERLINK", filename))
        else:
            issues.append(("P0", "EXTERNAL_RELATIONSHIP", filename))
    return issues


def validate_xlsx_zip(path):
    issues = []
    path = Path(path)
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        issues.append(("P0", "EXTENSION", "只允许上传 .xlsx 文件"))
    if path.stat().st_size > MAX_ZIP_BYTES:
        issues.append(("P0", "ZIP_SIZE", "压缩文件超过 50 MiB"))
    total = 0
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return [("P0", "BAD_ZIP", "文件不是有效 xlsx/OOXML 压缩包")]
    with zf:
        names = zf.namelist()
        if len(names) > MAX_ZIP_ENTRIES:
            issues.append(("P0", "ZIP_ENTRY_COUNT", "压缩包条目数量超过安全上限"))
        required = {"[Content_Types].xml", "xl/workbook.xml"}
        if not required.issubset(set(names)):
            issues.append(("P0", "OOXML", "缺少 Excel OOXML 必要结构"))
        seen_raw = set()
        seen_normalized = set()
        for info in zf.infolist():
            norm = _normalised_zip_name(info.filename)
            lower_norm = norm.lower()
            total += info.file_size
            if info.filename in seen_raw or lower_norm in seen_normalized:
                issues.append(("P0", "ZIP_DUPLICATE_ENTRY", info.filename))
            seen_raw.add(info.filename)
            seen_normalized.add(lower_norm)
            if _unsafe_zip_name(info.filename):
                issues.append(("P0", "ZIP_TRAVERSAL", info.filename))
            is_xml = lower_norm.endswith((".xml", ".rels"))
            if is_xml and info.file_size > MAX_XML_BYTES:
                issues.append(("P0", "XML_SIZE", info.filename))
            if lower_norm.endswith((".bin", ".vba", "vbaproject.bin")):
                issues.append(("P0", "MACRO_OR_OLE", info.filename))
            if lower_norm.startswith(
                (
                    "xl/externallinks/",
                    "xl/connections",
                    "xl/ctrlprops/",
                    "xl/embeddings/",
                    "xl/activex/",
                )
            ):
                issues.append(("P0", "BLOCKED_PART", info.filename))
            if lower_norm.startswith("xl/externallinks/"):
                issues.append(("P0", "EXTERNAL_LINK", "存在外部链接包"))
            if lower_norm.startswith("xl/connections"):
                issues.append(("P0", "CONNECTION", "存在外部连接"))
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > MAX_RATIO:
                issues.append(("P0", "ZIP_RATIO", info.filename))
        if total > MAX_UNZIPPED_BYTES:
            issues.append(("P0", "UNZIPPED_SIZE", "解压后超过 500 MiB"))
        if any(issue[0] == "P0" for issue in issues):
            return issues
        for info in zf.infolist():
            norm = _normalised_zip_name(info.filename)
            lower_norm = norm.lower()
            is_xml = lower_norm.endswith((".xml", ".rels"))
            if is_xml and info.file_size:
                try:
                    with zf.open(info) as fh:
                        raw = fh.read(MAX_XML_BYTES + 1)
                except (KeyError, RuntimeError, zipfile.BadZipFile, zlib.error):
                    issues.append(("P0", "ZIP_READ_ERROR", info.filename))
                    continue
                text = _decode_xml_for_scan(raw)
                if XML_DTD_RE.search(text):
                    issues.append(("P0", "XML_ENTITY", info.filename))
                issues.extend(_external_relationship_issues(info.filename, text))
    return issues


def summary_sheet_names(upload):
    template = upload.template
    if template and template.manifest_path:
        path = resolve_template_path(template.manifest_path)
        if not path.is_absolute():
            path = settings.BASE_DIR / path
        if path.exists():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            sheets = {
                report["sheet"]
                for report in manifest.get("reports", {}).values()
                if report.get("sheet")
            }
            if sheets:
                return sheets
    from budgeting.models import REPORTS

    return set(REPORTS.values())


def validate_upload_contract(upload, path):
    preflight_issues = validate_xlsx_zip(path)
    if any(issue[0] == "P0" for issue in preflight_issues):
        return preflight_issues
    issues = []
    if upload.template:
        from budgeting.excel.structure_v2 import validate_v2_structure

        try:
            issues.extend(validate_v2_structure(upload, path))
        except (
            ValueError,
            KeyError,
            OSError,
            ET.ParseError,
            zipfile.BadZipFile,
        ) as exc:
            issues.append(("P0", "V2_STRUCTURE_INVALID", f"模板结构无法校验：{exc}"))
    try:
        meta = read_sys_meta(path)
    except (KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        return [("P0", "SYS_META", f"无法读取 SYS_META：{exc}")]
    required = [
        "template_version",
        "budget_year",
        "project_code",
        "rule_version",
        "formula_manifest_hash",
        "project_signature",
    ]
    for key in required:
        if not meta.get(key):
            issues.append(("P0", "SYS_META_MISSING", f"SYS_META 缺少 {key}"))
    if issues:
        issues.append(
            (
                "P0",
                "TEMPLATE_SIGNATURE",
                "上传模板签名、年度、项目、版本或规则与当前周期不一致。",
            )
        )
    template = upload.template
    if not template:
        issues.append(("P0", "TEMPLATE_MISSING", "上传版本未绑定模板。"))
        return issues
    if meta.get("template_version") != template.version:
        issues.append(("P0", "TEMPLATE_VERSION", "上传模板版本与周期模板不一致。"))
    if str(meta.get("budget_year")) != str(upload.cycle.budget_year):
        issues.append(("P0", "BUDGET_YEAR", "上传模板年度与周期年度不一致。"))
    if meta.get("project_code") != upload.project.code:
        issues.append(("P0", "PROJECT_SIGNATURE", "上传模板项目与登录项目不一致。"))
    if meta.get("rule_version") != template.rule_version:
        issues.append(("P0", "RULE_VERSION", "上传模板规则版本不一致。"))
    if meta.get("formula_manifest_hash") != template.formula_manifest_hash:
        issues.append(("P0", "FORMULA_HASH", "上传模板公式指纹与平台模板不一致。"))
    try:
        payload = signing.loads(
            meta.get("project_signature", ""), salt="budget-template-v1"
        )
    except signing.BadSignature:
        issues.append(("P0", "PROJECT_SIGNATURE", "项目签名令牌无效。"))
    else:
        expected = {
            "project_code": upload.project.code,
            "budget_year": upload.cycle.budget_year,
            "cycle_id": upload.cycle_id,
            "template_version": template.version,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                issues.append(
                    (
                        "P0",
                        "PROJECT_SIGNATURE",
                        "项目签名令牌与登录项目、年度、周期或模板不一致。",
                    )
                )
                break
    formulas, digest = formula_manifest(path)
    template_path = resolve_template_path(template.file_path)
    if not template_path.is_absolute():
        template_path = settings.BASE_DIR / template_path
    summary_sheets = summary_sheet_names(upload)
    if template_path.exists():
        expected_formulas, _ = formula_manifest(template_path)
        formula_changed = formula_comparison_rows(formulas, summary_sheets) != formula_comparison_rows(expected_formulas, summary_sheets)
    else:
        formula_changed = digest != template.formula_manifest_hash
    if formula_changed:
        issues.append(("P0", "FORMULA_FINGERPRINT", "上传汇总表公式与模板清单不一致。"))
    for code, sheet in {
        "PL_TOTAL_WINE": "酒店损益总表（含名酒）",
        "PL_TOTAL_NOWINE": "酒店损益总表（不含名酒）",
        "PL_ZZ_WINE": "损益表（含名酒）（拆中智）",
        "PL_ZZ_NOWINE": "损益表（不含名酒）（拆中智）",
    }.items():
        if not workbook_has_sheet(path, sheet):
            issues.append(("P0", "REQUIRED_SHEET", f"{code} 缺少工作表：{sheet}"))
            issues.append(
                (
                    "P0",
                    "MISSING_REQUIRED_SHEET",
                    f"缺少固定管理报表工作表：{sheet}",
                    sheet,
                )
            )
    for error in cached_errors(path):
        issues.append(
            (
                "P0" if error["sheet"] in summary_sheets else "P2",
                "EXCEL_ERROR",
                f"发现 Excel 错误值 {error['value']}。",
                f"{error['sheet']}!{error['cell']}",
            )
        )
    if any(
        issue[1]
        in {
            "TEMPLATE_VERSION",
            "BUDGET_YEAR",
            "PROJECT_SIGNATURE",
            "RULE_VERSION",
            "FORMULA_HASH",
        }
        for issue in issues
    ):
        issues.append(
            (
                "P0",
                "TEMPLATE_SIGNATURE",
                "上传模板签名、年度、项目、版本或规则与当前周期不一致。",
            )
        )
    return issues


def read_sys_meta(path):
    meta = {}
    with zipfile.ZipFile(path) as zf:
        shared_strings = _shared_strings(zf)
        sheet_path = _sheet_path_by_name(zf, "SYS_META")
        if not sheet_path:
            raise KeyError("SYS_META")
        root = ET.fromstring(zf.read(sheet_path))
        rows = {}
        for cell in root.findall(".//m:c", NS):
            ref = cell.attrib.get("r", "")
            row = "".join(ch for ch in ref if ch.isdigit())
            col = "".join(ch for ch in ref if ch.isalpha())
            if row:
                rows.setdefault(int(row), {})[col] = _cell_text(cell, shared_strings)
        for cells in rows.values():
            if cells.get("A"):
                meta[cells["A"]] = cells.get("B", "")
    return meta


def workbook_has_sheet(path, sheet_name):
    with zipfile.ZipFile(path) as zf:
        return _sheet_path_by_name(zf, sheet_name) is not None


def _sheet_path_by_name(zf, sheet_name):
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.attrib["Id"]: _resolve_part_name("xl/workbook.xml", rel.attrib["Target"])
        for rel in rels
    }
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    for sheet in wb.findall("m:sheets/m:sheet", NS):
        if sheet.attrib.get("name") == sheet_name:
            return targets[sheet.attrib[REL_NS]]
    return None


def _shared_strings(zf):
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    return [_rich_text(si) for si in root.findall("m:si", NS)]


def _rich_text(element):
    return "".join(text.text or "" for text in element.findall(".//m:t", NS))


def _cell_text(cell, shared_strings=None):
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = cell.find("m:is", NS)
        return _rich_text(inline) if inline is not None else ""
    value = cell.find("m:v", NS)
    raw = value.text if value is not None and value.text is not None else ""
    if cell_type == "s":
        try:
            return (shared_strings or [])[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


def _sheet_name_map(zf):
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {}
    for relationship in rels:
        rid_to_target[relationship.attrib["Id"]] = _resolve_part_name(
            "xl/workbook.xml", relationship.attrib["Target"]
        )
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    out = {}
    for sheet in wb.findall("m:sheets/m:sheet", NS):
        rid = sheet.attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]
        out[rid_to_target[rid]] = {
            "name": sheet.attrib["name"],
            "state": sheet.attrib.get("state", "visible"),
        }
    return out


def formula_comparison_rows(rows, sheets):
    """Compare formulas while allowing observed Excel save-only lexical changes.

    Keep the package fingerprint unchanged. Tokenization protects string literals
    and INDIRECT arguments from reference/number normalization.
    """
    result = []
    for row in rows:
        if row["sheet"] not in sheets:
            continue
        text = row["formula"]
        # Tokenizer accepts uppercase error constants only; do not change text
        # literals or quoted sheet names that happen to contain the same text.
        lexical_text = re.sub(
            r'''"(?:[^"]|"")*"|'(?:[^']|'')*'|\#ref!''',
            lambda match: "#REF!" if match.group(0).lower() == "#ref!" else match.group(0),
            text, flags=re.I,
        )
        try:
            tokens = Tokenizer("=" + lexical_text).items
        except (TokenizerError, ValueError, IndexError):
            result.append(row)
            continue
        values = []
        for token in tokens:
            value = token.value
            if token.type == "OPERAND" and token.subtype == "NUMBER" and re.fullmatch(r"0+[0-9]+", value):
                value = value.lstrip("0") or "0"
            elif token.type == "OPERAND" and token.subtype == "ERROR":
                value = value.upper()
            elif token.type == "OPERAND" and token.subtype == "RANGE":
                value = re.sub(r"^'([A-Za-z_\u0080-\uffff][A-Za-z_0-9\u0080-\uffff]*)'!", r"\1!", value)
                prefix, separator, address = value.rpartition("!")
                if not separator:
                    address = value
                if re.fullmatch(r"\$?[A-Za-z]{1,3}\$?0*[1-9][0-9]*(?::\$?[A-Za-z]{1,3}\$?0*[1-9][0-9]*)?", address):
                    address = re.sub(r"([A-Za-z]\$?)0+([1-9][0-9]*)", r"\1\2", address)
                    value = prefix + separator + address
            values.append(value)
        attributes = dict(row["shared_attributes"])
        # Excel marks volatile formulas calculate-always on save. This flag
        # changes recalculation scheduling, not the expression or its extent.
        if attributes.get("ca") == "1":
            attributes.pop("ca")
        result.append(dict(row, formula="".join(values), shared_attributes=attributes))
    return result


def formula_manifest(path):
    rows = []
    digest_rows = []
    with zipfile.ZipFile(path) as zf:
        sheets = _sheet_name_map(zf)
        for name in zf.namelist():
            if not re.match(r"xl/worksheets/sheet\d+\.xml$", name):
                continue
            root = ET.fromstring(zf.read(name))
            sheet = sheets.get(name, {"name": name, "state": "visible"})
            shared_formulas = {}
            for cell in root.findall(".//m:c[m:f]", NS):
                f = cell.find("m:f", NS)
                digest_rows.append(
                    {
                        "sheet": sheet["name"],
                        "cell": cell.attrib.get("r"),
                        "formula": (f.text or "").strip(),
                        "shared_attributes": dict(sorted(f.attrib.items())),
                    }
                )
                text, attributes = _normalise_formula(
                    cell.attrib.get("r", ""), f, shared_formulas
                )
                rows.append(
                    {
                        "sheet": sheet["name"],
                        "cell": cell.attrib.get("r"),
                        "formula": text,
                        "shared_attributes": attributes,
                    }
                )
    rows.sort(key=lambda row: (row["sheet"], row["cell"]))
    digest_rows.sort(key=lambda row: (row["sheet"], row["cell"]))
    payload = json.dumps(
        digest_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    return rows, digest


def _resolve_part_name(source_part, target):
    target = target.split("#", 1)[0]
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    base = posixpath.dirname(source_part)
    return posixpath.normpath(posixpath.join(base, target))


def _translate_shared_formula(formula, source, target):
    try:
        translated = Translator(f"={formula}", origin=source).translate_formula(target)
    except (TranslatorError, TypeError, ValueError):
        return f"[UNTRANSLATABLE_SHARED_FORMULA:{source}->{target}:{formula}]"
    return translated[1:] if translated.startswith("=") else translated


def _normalise_formula(cell_ref, formula_node, shared_formulas):
    attributes = dict(sorted(formula_node.attrib.items()))
    formula_type = attributes.get("t")
    text = (formula_node.text or "").strip()
    if formula_type != "shared":
        return text, attributes
    shared_index = attributes.get("si", "")
    if text:
        shared_formulas[shared_index] = (text, cell_ref)
        return text, {}
    if shared_index in shared_formulas:
        base_formula, base_cell = shared_formulas[shared_index]
        return _translate_shared_formula(base_formula, base_cell, cell_ref), {}
    return f"[UNRESOLVED_SHARED_FORMULA:{shared_index}]", attributes


def _formula_string(value):
    return '"' + value.replace('"', '""') + '"'


def _replace_formula(text):
    upper = text.upper().strip()
    if not any(func in upper for func in PROPRIETARY_FUNCS):
        return text, False, None
    if upper.startswith("VIEW("):
        quoted = re.findall(r'"([^"]*)"', text)
        if quoted:
            return _formula_string(quoted[-1]), True, None
    if upper.startswith("SUBNM("):
        quoted = re.findall(r'"([^"]*)"', text)
        if len(quoted) >= 3:
            return _formula_string(quoted[2]), True, None
        if quoted:
            return _formula_string(quoted[-1]), True, None
    m = re.match(r"DBS\(([^,]+),", text, flags=re.I)
    if m and m.group(1).strip().upper() == "#REF!":
        return "", True, None
    if m and re.fullmatch(
        r"(\$?[A-Z]{1,3}\$?\d+|[A-Za-z0-9_\u4e00-\u9fff ]+!\$?[A-Z]{1,3}\$?\d+)",
        m.group(1).strip(),
    ):
        return m.group(1).strip(), True, None
    return "", True, "专有函数无法自动证明为本地引用，已置空并阻断发布"


def purify_workbook(source, target, meta):
    target = Path(target)
    shutil.copy2(source, target)
    tmp = target.with_suffix(".tmp.xlsx")
    report = {
        "removed_parts": [],
        "blocked": [],
        "replaced_formulas": 0,
        "blanked_formulas": 0,
    }
    with (
        zipfile.ZipFile(target) as zin,
        zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout,
    ):
        sheets = _sheet_name_map(zin)
        for info in zin.infolist():
            if info.filename.startswith(BLOCKED_PREFIXES):
                report["removed_parts"].append(info.filename)
                continue
            data = zin.read(info.filename)
            if re.match(r"xl/worksheets/sheet\d+\.xml$", info.filename):
                root = ET.fromstring(data)
                sheet = sheets.get(info.filename, {"name": info.filename})
                changed = False
                for cell in root.findall(".//m:c[m:f]", NS):
                    f = cell.find("m:f", NS)
                    original = f.text or ""
                    new_text, did, blocker = _replace_formula(original)
                    if did:
                        f.text = new_text
                        changed = True
                        report["replaced_formulas"] += 1
                        if blocker:
                            report["blanked_formulas"] += 1
                            report["blocked"].append(
                                {
                                    "part": info.filename,
                                    "sheet": sheet["name"],
                                    "cell": cell.attrib.get("r", ""),
                                    "original_formula": original,
                                    "reason": blocker,
                                }
                            )
                if changed:
                    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            zout.writestr(info, data)
    tmp.replace(target)
    formulas, digest = formula_manifest(target)
    report["formula_manifest_hash"] = digest
    report["formula_count"] = len(formulas)
    report["proprietary_remaining"] = [
        x
        for x in formulas
        if any(fn in x["formula"].upper() for fn in PROPRIETARY_FUNCS)
    ]
    report["meta"] = meta
    return report, formulas


def repair_drawing_namespaces(path):
    """Repair only the known misplaced ``avLst`` drawing child in-place.

    Some generated drawings put an unprefixed ``avLst`` directly below an
    ``a:prstGeom`` element.  When the drawing root uses the spreadsheet
    drawing namespace as its default, that child is consequently in the wrong
    namespace.  The repair changes only that direct child and atomically
    replaces the workbook after the replacement archive has been closed and
    synced.  A workbook without a matching node is left byte-for-byte
    untouched.

    Returns the repaired OOXML part names after a successful replacement.
    """

    source = Path(path)
    pending = {}
    drawing_pattern = re.compile(r"xl/drawings/drawing\d+\.xml$")
    malformed_av_lst = f"{{{DRAWING_NS}}}avLst"
    correct_av_lst = f"{{{DRAWING_MAIN_NS}}}avLst"
    preset_geometry = f"{{{DRAWING_MAIN_NS}}}prstGeom"

    with zipfile.ZipFile(source, "r") as zin:
        for info in zin.infolist():
            if not drawing_pattern.fullmatch(info.filename):
                continue
            raw = zin.read(info)
            root = ET.fromstring(raw)
            changed = False
            for geometry in root.iter(preset_geometry):
                for child in list(geometry):
                    if child.tag == malformed_av_lst:
                        child.tag = correct_av_lst
                        changed = True
            if changed:
                pending[info.filename] = ET.tostring(
                    root,
                    encoding="utf-8",
                    xml_declaration=True,
                )

    if not pending:
        return []

    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{source.name}.",
        suffix=".tmp",
        dir=str(source.parent),
    )
    os.close(temp_fd)
    temp_path = Path(temp_name)
    try:
        with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(
            temp_path, "w"
        ) as zout:
            zout.comment = zin.comment
            for info in zin.infolist():
                data = pending.get(info.filename)
                if data is None:
                    data = zin.read(info)
                # Reusing ZipInfo retains timestamps, compression, flags,
                # permissions, extra fields, and entry ordering.
                zout.writestr(info, data)

        os.chmod(temp_path, source.stat().st_mode & 0o7777)
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, source)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise

    return sorted(pending)


def workbook_manifest(path, meta=None):
    with zipfile.ZipFile(path) as zf:
        sheets = list(_sheet_name_map(zf).values())
    formulas, digest = formula_manifest(path)
    funcs = sorted(
        {
            m.group(1).upper()
            for item in formulas
            for m in [re.match(r"([A-Z][A-Z0-9.]*)\(", item["formula"].strip(), re.I)]
            if m
        }
    )
    return {
        "metadata": meta or {},
        "sheets": sheets,
        "formulas": formulas,
        "report_mappings": [
            {"report_code": code, "sheet": sheet}
            for code, sheet in {
                "PL_TOTAL_WINE": "酒店损益总表（含名酒）",
                "PL_TOTAL_NOWINE": "酒店损益总表（不含名酒）",
                "PL_ZZ_WINE": "损益表（含名酒）（拆中智）",
                "PL_ZZ_NOWINE": "损益表（不含名酒）（拆中智）",
            }.items()
        ],
        "allowed_functions": funcs,
        "input_ranges": [],
        "system_ranges": ["SYS_META"],
        "hash": digest,
    }


def cached_errors(path):
    errors = []
    with zipfile.ZipFile(path) as zf:
        sheets = _sheet_name_map(zf)
        for name in zf.namelist():
            if not re.match(r"xl/worksheets/sheet\d+\.xml$", name):
                continue
            root = ET.fromstring(zf.read(name))
            sheet = sheets.get(name, {"name": name})
            for cell in root.findall(".//m:c[@t='e']", NS):
                value = cell.find("m:v", NS)
                errors.append(
                    {
                        "sheet": sheet["name"],
                        "cell": cell.attrib.get("r", ""),
                        "value": value.text if value is not None else "",
                    }
                )
    return errors
