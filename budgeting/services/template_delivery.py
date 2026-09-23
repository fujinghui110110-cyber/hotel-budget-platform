import json
import re
import zipfile
from xml.etree import ElementTree as ET

from django.conf import settings
from budgeting.services.template_paths import resolve_template_path
from django.core import signing
from django.utils import timezone


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS, "pr": PACKAGE_REL_NS}
ET.register_namespace("", MAIN_NS)
ET.register_namespace("r", REL_NS)


def signed_template_copy(template, project, cycle, *, source_path=None, preserve_inputs=False):
    from openpyxl import load_workbook
    from budgeting.services.plan_history import ensure_plan, current_binding, history_metadata
    from budgeting.services.history_workbook import template_cell_payload

    source = source_path if source_path is not None else resolve_template_path(template.file_path)
    target_dir = settings.BUDGET_STORAGE_ROOT / "template_downloads" / project.code
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{timezone.now().strftime('%Y%m%d%H%M%S%f')}_{source.name}"
    manifest = json.loads((resolve_template_path(template.manifest_path)).read_text(encoding="utf-8")) if template.manifest_path else {}
    binding = current_binding(ensure_plan(cycle), project)
    history_meta = history_metadata(binding) if binding else {}
    locked_cells = {}
    if binding:
        workbook = load_workbook(source, read_only=True, data_only=False)
        try:
            for item in template_cell_payload(binding, manifest, workbook):
                if item["mapping_missing"] or item["excel_value"] is None:
                    raise ValueError("已确认历史缺少可回填映射或有效比例，不能下载不完整的锁定模板。")
                locked_cells.setdefault(item["sheet"], {})[item["cell"]] = item["excel_value"]
        finally:
            workbook.close()
    token = signing.dumps(
        {
            "project_code": project.code,
            "budget_year": cycle.budget_year,
            "cycle_id": cycle.id,
            "template_version": template.version,
            **history_meta,
        },
        salt="budget-template-v1",
    )
    meta = {
        "template_version": template.version,
        "budget_year": str(cycle.budget_year),
        "project_code": project.code,
        "rule_version": template.rule_version,
        "formula_manifest_hash": template.formula_manifest_hash,
        "project_signature": token,
        **history_meta,
    }
    input_cells = None
    if template.manifest_path:
        manifest = json.loads((resolve_template_path(template.manifest_path)).read_text(encoding="utf-8"))
        if manifest.get("management_v2"):
            input_cells = manifest["input_cells"]
    _inject_sys_meta(source, target, meta, input_cells=input_cells, locked_cells=locked_cells, preserve_inputs=preserve_inputs)
    return target


def _inject_sys_meta(source, target, meta, input_cells=None, locked_cells=None, preserve_inputs=False):
    with zipfile.ZipFile(source, "r") as zin:
        from budgeting.excel.ooxml import _sheet_name_map
        sheet_names = _sheet_name_map(zin) if input_cells is not None or locked_cells else {}
        sheet_path = _sys_meta_sheet_path(zin)
        additions = {}
        if not sheet_path:
            sheet_path, additions = _metadata_sheet_parts(zin)

        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = additions.get(info.filename, zin.read(info.filename))
                if info.filename == sheet_path:
                    data = _patched_sheet_xml(data, meta)
                elif re.match(r"xl/worksheets/sheet\d+\.xml$", info.filename):
                    sheet_name = sheet_names.get(info.filename, {}).get("name")
                    if preserve_inputs:
                        pass
                    elif input_cells is None:
                        data = _blank_input_cells(data)
                    else:
                        sheet_name = sheet_names.get(info.filename, {}).get("name")
                        data = _blank_input_cells(data, allowed=set(input_cells.get(sheet_name, [])))
                    if locked_cells and sheet_name in locked_cells:
                        data = _fill_locked_cells(data, locked_cells[sheet_name])
                zout.writestr(info, data)
            if sheet_path in additions:
                zout.writestr(sheet_path, _patched_sheet_xml(additions[sheet_path], meta))


def _metadata_sheet_parts(package):
    """Add only the hidden identity sheet, preserving formulas and cached results."""
    workbook = ET.fromstring(package.read("xl/workbook.xml"))
    relations = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
    types = ET.fromstring(package.read("[Content_Types].xml"))
    sheets = workbook.find("m:sheets", NS)
    if sheets is None:
        raise ValueError("工作簿没有可用工作表。")
    number = max((int(s.get("sheetId", "0")) for s in sheets), default=0) + 1
    names = set(package.namelist())
    while f"xl/worksheets/sheet{number}.xml" in names:
        number += 1
    relationship_id = f"rIdBudgetMetadata{number}"
    while any(r.get("Id") == relationship_id for r in relations):
        relationship_id += "x"
    sheet_path = f"xl/worksheets/sheet{number}.xml"
    ET.SubElement(sheets, f"{{{MAIN_NS}}}sheet", {
        "name": "SYS_META", "sheetId": str(number), "state": "hidden",
        f"{{{REL_NS}}}id": relationship_id,
    })
    ET.SubElement(relations, f"{{{PACKAGE_REL_NS}}}Relationship", {
        "Id": relationship_id, "Type": REL_NS + "/worksheet",
        "Target": f"worksheets/sheet{number}.xml",
    })
    ET.SubElement(types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override", {
        "PartName": "/" + sheet_path,
        "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
    })
    sheet = ET.Element(f"{{{MAIN_NS}}}worksheet")
    ET.SubElement(sheet, f"{{{MAIN_NS}}}sheetData")
    return sheet_path, {name: ET.tostring(root, encoding="utf-8", xml_declaration=True)
                        for name, root in (("xl/workbook.xml", workbook),
                                           ("xl/_rels/workbook.xml.rels", relations),
                                           ("[Content_Types].xml", types), (sheet_path, sheet))}


def _fill_locked_cells(data, values):
    root = ET.fromstring(data)
    sheet_data = root.find("m:sheetData", NS)
    if sheet_data is None:
        sheet_data = ET.SubElement(root, f"{{{MAIN_NS}}}sheetData")
    rows = {int(row.attrib["r"]): row for row in sheet_data.findall("m:row", NS)}
    for ref, value in values.items():
        number = int(re.search(r"\d+$", ref).group())
        row = rows.get(number)
        if row is None:
            row = ET.SubElement(sheet_data, f"{{{MAIN_NS}}}row", {"r": str(number)})
            rows[number] = row
        cell = next((c for c in row.findall("m:c", NS) if c.get("r") == ref), None)
        if cell is None:
            cell = ET.SubElement(row, f"{{{MAIN_NS}}}c", {"r": ref})
        # Preserve controlled formulas; populate their cache for historical comparison.
        for child in list(cell):
            if child.tag != f"{{{MAIN_NS}}}f":
                cell.remove(child)
        cell.attrib.pop("t", None)
        ET.SubElement(cell, f"{{{MAIN_NS}}}v").text = str(value)
        row[:] = sorted(row, key=lambda c: _cell_sort_key(c.get("r", "")))
    sheet_data[:] = sorted(sheet_data, key=lambda r: int(r.get("r", "0")))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _sys_meta_sheet_path(zf):
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.attrib["Id"]: rel.attrib["Target"].lstrip("/")
        for rel in rels.findall("pr:Relationship", NS)
    }
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        if sheet.attrib.get("name") == "SYS_META":
            rel_id = sheet.attrib[f"{{{REL_NS}}}id"]
            target = targets[rel_id]
            return target if target.startswith("xl/") else f"xl/{target}"
    return None


def _blank_input_cells(data, allowed=None):
    root = ET.fromstring(data)
    for cell in root.findall(".//m:c", NS):
        if cell.find("m:f", NS) is not None:
            continue
        if allowed is not None:
            if cell.attrib.get("r") not in allowed:
                continue
            for child in list(cell):
                if child.tag in (f"{{{MAIN_NS}}}v", f"{{{MAIN_NS}}}is"):
                    cell.remove(child)
            cell.attrib.pop("t", None)
            continue
        if cell.attrib.get("t") not in (None, "n"):
            continue
        v = cell.find("m:v", NS)
        if v is not None:
            cell.remove(v)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _patched_sheet_xml(data, meta):
    root = ET.fromstring(data)
    sheet_data = root.find("m:sheetData", NS)
    if sheet_data is None:
        sheet_data = ET.SubElement(root, f"{{{MAIN_NS}}}sheetData")
    rows = {int(row.attrib["r"]): row for row in sheet_data.findall("m:row", NS) if row.attrib.get("r", "").isdigit()}
    for idx, (key, value) in enumerate(meta.items(), start=1):
        row = rows.get(idx)
        if row is None:
            row = ET.SubElement(sheet_data, f"{{{MAIN_NS}}}row", {"r": str(idx)})
            rows[idx] = row
        _set_inline_string(row, f"A{idx}", key)
        _set_inline_string(row, f"B{idx}", value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _set_inline_string(row, ref, value):
    cell = None
    for existing in row.findall("m:c", NS):
        if existing.attrib.get("r") == ref:
            cell = existing
            break
    if cell is None:
        cell = ET.SubElement(row, f"{{{MAIN_NS}}}c", {"r": ref})
    for child in list(cell):
        cell.remove(child)
    cell.attrib.clear()
    cell.attrib.update({"r": ref, "t": "inlineStr"})
    inline = ET.SubElement(cell, f"{{{MAIN_NS}}}is")
    text = ET.SubElement(inline, f"{{{MAIN_NS}}}t")
    text.text = str(value)
    row[:] = sorted(list(row), key=lambda c: _cell_sort_key(c.attrib.get("r", "")))


def _cell_sort_key(ref):
    letters = "".join(ch for ch in ref if ch.isalpha())
    number = int("".join(ch for ch in ref if ch.isdigit()) or 0)
    col = 0
    for ch in letters:
        col = col * 26 + ord(ch.upper()) - 64
    return number, col
