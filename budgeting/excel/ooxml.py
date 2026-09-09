import hashlib
import json
import posixpath
import re
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from django.conf import settings
from django.core import signing

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
MAX_RATIO = 100
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_xlsx_zip(path):
    issues = []
    path = Path(path)
    if path.suffix.lower() != ".xlsx":
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
        required = {"[Content_Types].xml", "xl/workbook.xml"}
        if not required.issubset(set(names)):
            issues.append(("P0", "OOXML", "缺少 Excel OOXML 必要结构"))
        for info in zf.infolist():
            norm = posixpath.normpath(info.filename)
            total += info.file_size
            if norm.startswith("../") or norm.startswith("/"):
                issues.append(("P0", "ZIP_TRAVERSAL", info.filename))
            if info.filename.lower().endswith(
                (".bin", ".vba", ".vbaProject.bin".lower())
            ):
                issues.append(("P0", "MACRO_OR_OLE", info.filename))
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > MAX_RATIO:
                issues.append(("P0", "ZIP_RATIO", info.filename))
        if total > MAX_UNZIPPED_BYTES:
            issues.append(("P0", "UNZIPPED_SIZE", "解压后超过 500 MiB"))
        if any(name.startswith("xl/externalLinks/") for name in names):
            issues.append(("P0", "EXTERNAL_LINK", "存在外部链接包"))
        if any(name.startswith("xl/connections") for name in names):
            issues.append(("P0", "CONNECTION", "存在外部连接"))
    return issues


def summary_sheet_names(upload):
    template = upload.template
    if template and template.manifest_path:
        path = Path(template.manifest_path)
        if not path.is_absolute():
            path = settings.BASE_DIR / path
        if path.exists():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            sheets = {report["sheet"] for report in manifest.get("reports", {}).values() if report.get("sheet")}
            if sheets:
                return sheets
    from budgeting.models import REPORTS
    return set(REPORTS.values())


def validate_upload_contract(upload, path):
    issues = []
    if upload.template:
        from budgeting.excel.structure_v2 import validate_v2_structure
        try:
            issues.extend(validate_v2_structure(upload, path))
        except (ValueError, KeyError, OSError, ET.ParseError, zipfile.BadZipFile) as exc:
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
    template_path = Path(template.file_path)
    if not template_path.is_absolute():
        template_path = settings.BASE_DIR / template_path
    summary_sheets = summary_sheet_names(upload)
    if template_path.exists():
        expected_formulas, _ = formula_manifest(template_path)
        formula_changed = [row for row in formulas if row["sheet"] in summary_sheets] != [
            row for row in expected_formulas if row["sheet"] in summary_sheets
        ]
    else:
        formula_changed = digest != template.formula_manifest_hash
    if formula_changed:
        issues.append(
            ("P0", "FORMULA_FINGERPRINT", "上传汇总表公式与模板清单不一致。")
        )
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
                rows.setdefault(int(row), {})[col] = _cell_text(cell)
        for cells in rows.values():
            if cells.get("A"):
                meta[cells["A"]] = cells.get("B", "")
    return meta


def workbook_has_sheet(path, sheet_name):
    with zipfile.ZipFile(path) as zf:
        return _sheet_path_by_name(zf, sheet_name) is not None


def _sheet_path_by_name(zf, sheet_name):
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.attrib["Id"]: rel.attrib["Target"].lstrip("/") for rel in rels}
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rel_key = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for sheet in wb.findall("m:sheets/m:sheet", NS):
        if sheet.attrib.get("name") == sheet_name:
            target = targets[sheet.attrib[rel_key]]
            return target if target.startswith("xl/") else f"xl/{target}"
    return None


def _cell_text(cell):
    if cell.attrib.get("t") == "inlineStr":
        text = cell.find("m:is/m:t", NS)
        return text.text if text is not None and text.text is not None else ""
    value = cell.find("m:v", NS)
    return value.text if value is not None and value.text is not None else ""


def _sheet_name_map(zf):
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {}
    for relationship in rels:
        target = relationship.attrib["Target"].lstrip("/")
        rid_to_target[relationship.attrib["Id"]] = (
            target if target.startswith("xl/") else f"xl/{target}"
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


def formula_manifest(path):
    rows = []
    with zipfile.ZipFile(path) as zf:
        sheets = _sheet_name_map(zf)
        for name in zf.namelist():
            if not re.match(r"xl/worksheets/sheet\d+\.xml$", name):
                continue
            root = ET.fromstring(zf.read(name))
            sheet = sheets.get(name, {"name": name, "state": "visible"})
            for cell in root.findall(".//m:c[m:f]", NS):
                f = cell.find("m:f", NS)
                text = (f.text or "").strip()
                rows.append(
                    {
                        "sheet": sheet["name"],
                        "cell": cell.attrib.get("r"),
                        "formula": text,
                        "shared_attributes": dict(sorted(f.attrib.items())),
                    }
                )
    rows.sort(key=lambda row: (row["sheet"], row["cell"]))
    payload = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    return rows, digest


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
