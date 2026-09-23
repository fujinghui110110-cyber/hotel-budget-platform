#!/usr/bin/env python3
from __future__ import annotations
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from xml.etree import ElementTree as ET
from openpyxl.formula.translate import Translator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE = Path(os.getenv("SOURCE_WORKBOOK", ROOT / "source" / "template.xlsx"))
EXPECTED = "bd62ff23f236b373c5f2cf38b146b7e7e5f099b38560c191fbb1ad51bf8141dc"
M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
C = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {"m": M}
ET.register_namespace("", M)
ET.register_namespace("r", R)
RENAMES = {
    "酒店损益总表 (不含名酒)": "酒店损益总表（不含名酒）",
    "损益表 (含名酒)（拆中智）": "损益表（含名酒）（拆中智）",
    "损益表 (不含名酒)（拆中智）": "损益表（不含名酒）（拆中智）",
}
REPORTS = {
    "PL_TOTAL_WINE": ("酒店损益总表（含名酒）", "total"),
    "PL_TOTAL_NOWINE": ("酒店损益总表（不含名酒）", "total"),
    "PL_ZZ_WINE": ("损益表（含名酒）（拆中智）", "zz"),
    "PL_ZZ_NOWINE": ("损益表（不含名酒）（拆中智）", "zz"),
}

TOTAL_CONTRACT = {
    "rows": range(23, 108),
    "annual_column": "J",
    "month_columns": tuple("LMNOPQRSTUVW"),
    "label_column": "H",
    "count_sum": {24, 25, 26, 27, 28, 34, 35, 36},
    "average": {23, 39},
    "ratio": {29: (28, 24)},
    "derived": {30: (41, 28), 31: (41, 24), 37: (49, 35), 38: (45, 35)},
    "exclude": set(),
}
ZZ_CONTRACT = {
    "rows": range(9, 145),
    "annual_column": "S",
    "month_columns": tuple("FGHIJKLMNOPQ"),
    "label_column": "R",
    "count_sum": {10, 11, 17},
    "average": {9, 19},
    "ratio": {12: (11, 10)},
    "derived": {13: (21, 11), 14: (21, 10), 18: (30, 17)},
    "exclude": {47, 58, 66, 91, 104, 132, 137},
}
REPORT_CONTRACTS = {
    "PL_TOTAL_WINE": TOTAL_CONTRACT,
    "PL_TOTAL_NOWINE": TOTAL_CONTRACT,
    "PL_ZZ_WINE": ZZ_CONTRACT,
    "PL_ZZ_NOWINE": ZZ_CONTRACT,
}
PREFIXES = (
    "xl/externalLinks/",
    "xl/connections",
    "xl/embeddings/",
    "xl/activeX/",
    "xl/ctrlProps/",
)
ERRORS = {
    "#N/A",
    "#VALUE!",
    "#REF!",
    "#DIV/0!",
    "#NAME?",
    "#NUM!",
    "#NULL!",
    "#SPILL!",
    "#CALC!",
}
CRE = re.compile(r"^([A-Z]{1,3})(\d+)$")
FRE = re.compile(r"(?<![A-Z0-9_.])([A-Z][A-Z0-9_.]*)\s*\(", re.I)


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1048576), b""):
            h.update(b)
    return h.hexdigest()


def write(p, x):
    p.write_text(json.dumps(x, ensure_ascii=False, indent=2) + "\n", "utf-8")


def pkg(base, target):
    return (
        target.lstrip("/")
        if target.startswith("/")
        else posixpath.normpath(posixpath.join(posixpath.dirname(base), target))
    )


def sheets(z):
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rel = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rm = {x.attrib["Id"]: pkg("xl/workbook.xml", x.attrib["Target"]) for x in rel}
    return wb, [
        (x, rm[x.attrib[f"{{{R}}}id"]]) for x in wb.findall("m:sheets/m:sheet", NS)
    ]


def rename_formula(s):
    s = s or ""
    for a, b in RENAMES.items():
        s = s.replace(f"'{a}'!", f"'{b}'!").replace(f"{a}!", f"{b}!")
    return s


def replace_proprietary(text):
    upper = text.upper().strip()
    if upper.startswith("VIEW("):
        quoted = re.findall(r'"([^"]*)"', text)
        return ('"' + (quoted[-1] if quoted else "").replace('"', '""') + '"'), True, None
    if upper.startswith("SUBNM("):
        quoted = re.findall(r'"([^"]*)"', text)
        return (
            ('"' + (quoted[2] if len(quoted) >= 3 else quoted[-1] if quoted else "").replace('"', '""') + '"'),
            True,
            None,
        )
    if upper.startswith("DBS("):
        match = re.match(r"DBS\(([^,]+),", text, flags=re.I)
        if not match:
            return "", True, "DBS first argument could not be parsed"
        first = match.group(1).strip()
        if first.upper() == "#REF!":
            return "", True, None
        if re.fullmatch(
            r"(?:\$?[A-Z]{1,3}\$?\d+|(?:'[^']+'|[A-Za-z0-9_\u4e00-\u9fff ]+)!\$?[A-Z]{1,3}\$?\d+)",
            first,
        ):
            return first, True, None
        return "", True, "DBS first argument is not a local cell reference"
    return text, False, None


def formula(s):
    n, ch, block = replace_proprietary(rename_formula(s))
    return rename_formula(n), ch, block


def external_rel(x):
    text = " ".join(x.attrib.values()).lower()
    return x.attrib.get("TargetMode", "").lower() == "external" or any(
        k in text
        for k in ("externallink", "connection", "oleobject", "activex", "external")
    )


def clean_rels(data):
    root = ET.fromstring(data)
    removed = []
    for x in list(root):
        if external_rel(x):
            removed.append(dict(x.attrib))
            root.remove(x)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), removed


def clean_names(wb, names):
    parent = wb.find("m:definedNames", NS)
    removed = []
    if parent is None:
        return removed
    for x in list(parent):
        t = rename_formula(x.text)
        bad = next(
            (q for q in ("#REF!", "DBS(", "VIEW(", "SUBNM(") if q in t.upper()), None
        )
        reason = (
            "invalid_reference"
            if bad
            else "external_reference"
            if re.search(r"\[[^]]+\]", t)
            else None
        )
        if (
            not reason
            and x.attrib.get("localSheetId", "").isdigit()
            and int(x.attrib["localSheetId"]) >= len(names)
        ):
            reason = "invalid_local_sheet_id"
        if reason:
            removed.append(
                {"name": x.attrib.get("name"), "value": x.text, "reason": reason}
            )
            parent.remove(x)
        else:
            x.text = t
    return removed


def meta_xml():
    root = ET.Element(f"{{{M}}}worksheet")
    ET.SubElement(root, f"{{{M}}}dimension", {"ref": "A1:B6"})
    views = ET.SubElement(root, f"{{{M}}}sheetViews")
    ET.SubElement(views, f"{{{M}}}sheetView", {"workbookViewId": "0"})
    data = ET.SubElement(root, f"{{{M}}}sheetData")
    for i, (k, v) in enumerate(
        (
            ("template_version", "V1"),
            ("budget_year", "2026"),
            ("project_code", ""),
            ("rule_version", "R1"),
            ("formula_manifest_hash", ""),
            ("signature_token", ""),
        ),
        1,
    ):
        row = ET.SubElement(data, f"{{{M}}}row", {"r": str(i)})
        for col, val in (("A", k), ("B", v)):
            c = ET.SubElement(row, f"{{{M}}}c", {"r": f"{col}{i}", "t": "inlineStr"})
            ins = ET.SubElement(c, f"{{{M}}}is")
            ET.SubElement(ins, f"{{{M}}}t").text = val
    ET.SubElement(
        root,
        f"{{{M}}}sheetProtection",
        {"sheet": "1", "objects": "1", "scenarios": "1"},
    )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def translate(text, origin, target):
    try:
        return (
            Translator("=" + text, origin=origin).translate_formula(target).lstrip("=")
        )
    except Exception:
        return text


def enforce_strict_annual_formulas(root, name, report):
    report_code = next(
        (code for code, (sheet, _) in REPORTS.items() if sheet == name), None
    )
    if not report_code:
        return
    contract = REPORT_CONTRACTS[report_code]
    cells = {
        cell.attrib.get("r"): cell for cell in root.findall(".//m:c", NS)
    }
    for row in contract["rows"]:
        if row in contract["exclude"]:
            continue
        annual = cells.get(f"{contract['annual_column']}{row}")
        month_cells = [cells.get(f"{column}{row}") for column in contract["month_columns"]]
        if annual is None or not any(
            cell is not None
            and (
                cell.find("m:f", NS) is not None
                or (
                    cell.find("m:v", NS) is not None
                    and cell.find("m:v", NS).text not in (None, "")
                )
            )
            for cell in month_cells
        ):
            continue
        value = annual.find("m:v", NS)
        formula_node = annual.find("m:f", NS)
        if formula_node is None:
            formula_node = ET.Element(f"{{{M}}}f")
            annual.insert(0, formula_node)
        formula_node.attrib.clear()
        month_range = (
            f"{contract['month_columns'][0]}{row}:"
            f"{contract['month_columns'][-1]}{row}"
        )
        if row in contract["count_sum"]:
            formula_node.text = f"SUM({month_range})"
        elif row in contract["average"]:
            formula_node.text = f"IF(COUNT({month_range})=0,0,ROUND(AVERAGE({month_range}),0))"
        elif row in contract["ratio"]:
            numerator_row, denominator_row = contract["ratio"][row]
            annual_column = contract["annual_column"]
            formula_node.text = (
                f"IF({annual_column}{denominator_row}=0,0,"
                f"{annual_column}{numerator_row}/{annual_column}{denominator_row})"
            )
        elif row in contract["derived"]:
            numerator_row, denominator_row = contract["derived"][row]
            annual_column = contract["annual_column"]
            formula_node.text = (
                f"ROUND(IF({annual_column}{denominator_row}=0,0,"
                f"{annual_column}{numerator_row}/{annual_column}{denominator_row}),2)"
            )
        else:
            if report_code.startswith("PL_ZZ") and row == 124:
                annual_column = contract["annual_column"]
                formula_node.text = (
                    f"ROUND({annual_column}113+{annual_column}121-"
                    f"SUM({annual_column}115:{annual_column}120,{annual_column}122)+"
                    f"{annual_column}123,2)"
                )
            else:
                rounded_months = ",".join(
                    f"ROUND({column}{row},2)"
                    for column in contract["month_columns"]
                )
                formula_node.text = f"ROUND(SUM({rounded_months}),2)"
        if value is not None:
            annual.remove(value)
        report["annual_formulas_rewritten"].append(f"{name}!{annual.attrib['r']}")


def transform_sheet(data, name, guards, report):
    root = ET.fromstring(data)
    local = {c: f for (s, c), f in guards.items() if s == name}
    groups = defaultdict(list)
    for c in root.findall(".//m:c[m:f]", NS):
        f = c.find("m:f", NS)
        if f.attrib.get("t") == "shared":
            groups[f.attrib.get("si")].append((c, f))
    normalized = set()
    for si, members in groups.items():
        if any(c.attrib.get("r") in local for c, _ in members):
            normalized.add(si)
            mc, mf = next((x for x in members if x[1].text), members[0])
            base, ch, block = formula(mf.text)
            report["proprietary_replaced"] += int(ch)
            for c, f in members:
                ref = c.attrib["r"]
                text = local.get(ref) or translate(base, mc.attrib["r"], ref)
                if ref in local and not text.upper().startswith("IFERROR("):
                    text = f"IFERROR({text},0)"
                f.attrib.clear()
                f.text = text
            report["normalized_shared_groups"].append(
                {"sheet": name, "si": si, "cells": [c.attrib["r"] for c, _ in members]}
            )
    for c in root.findall(".//m:c", NS):
        ref = c.attrib.get("r", "")
        f = c.find("m:f", NS)
        v = c.find("m:v", NS)
        if name == "A1客房收入(新)" and ref in {"AL44", "AM44"}:
            for child in list(c):
                c.remove(child)
            c.attrib.pop("t", None)
            report["blanked_unused"].append(f"{name}!{ref}")
            continue
        if f is not None:
            if not (f.attrib.get("t") == "shared" and f.attrib.get("si") in normalized):
                text, ch, block = formula(f.text)
                report["proprietary_replaced"] += int(ch)
                f.text = text
                if block:
                    report["replacement_blockers"].append(
                        {"sheet": name, "cell": ref, "reason": block}
                    )
                if ref in local:
                    text = local[ref] or text
                    f.attrib.clear()
                    f.text = (
                        text
                        if text.upper().startswith("IFERROR(")
                        else f"IFERROR({text},0)"
                    )
            if v is not None:
                c.remove(v)
            c.attrib.pop("t", None)
        elif c.attrib.get("t") == "e" or (v is not None and v.text in ERRORS):
            if v is not None:
                c.remove(v)
            c.attrib.pop("t", None)
            report["literal_errors_blanked"].append(f"{name}!{ref}")
    for tag in ("oleObjects", "controls"):
        for node in root.findall(f"m:{tag}", NS):
            root.remove(node)
    enforce_strict_annual_formulas(root, name, report)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build(source, target, guards=None):
    guards = guards or {}
    report = {
        "removed_parts": [],
        "removed_relationships": [],
        "removed_defined_names": [],
        "proprietary_replaced": 0,
        "replacement_blockers": [],
        "normalized_shared_groups": [],
        "annual_formulas_rewritten": [],
        "blanked_unused": [],
        "literal_errors_blanked": [],
    }
    temp = target.with_suffix(".building.xlsx")
    with zipfile.ZipFile(source) as zin:
        wb, ss = sheets(zin)
        old_by_part = {p: s.attrib["name"] for s, p in ss}
        for s, _ in ss:
            s.attrib["name"] = RENAMES.get(s.attrib["name"], s.attrib["name"])
        names = [s.attrib["name"] for s, _ in ss]
        report["removed_defined_names"] = clean_names(wb, names)
        for x in wb.findall("m:externalReferences", NS):
            wb.remove(x)
        rel = ET.fromstring(zin.read("xl/_rels/workbook.xml.rels"))
        used = {x.attrib["Id"] for x in rel}
        i = 1
        while f"rId{i}" in used:
            i += 1
        nums = [
            int(m.group(1))
            for n in zin.namelist()
            if (m := re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", n))
        ]
        num = max(nums) + 1
        rid = f"rId{i}"
        part = f"xl/worksheets/sheet{num}.xml"
        ET.SubElement(
            rel,
            f"{{{P}}}Relationship",
            {
                "Id": rid,
                "Type": f"{R}/worksheet",
                "Target": f"worksheets/sheet{num}.xml",
            },
        )
        sn = wb.find("m:sheets", NS)
        ET.SubElement(
            sn,
            f"{{{M}}}sheet",
            {
                "name": "SYS_META",
                "sheetId": str(max(int(x.attrib["sheetId"]) for x in sn) + 1),
                "state": "hidden",
                f"{{{R}}}id": rid,
            },
        )
        removed = {n for n in zin.namelist() if n.startswith(PREFIXES)} | (
            {"xl/calcChain.xml"} if "xl/calcChain.xml" in zin.namelist() else set()
        )
        report["removed_parts"] = sorted(removed)
        ct = ET.fromstring(zin.read("[Content_Types].xml"))
        for x in list(ct):
            if x.attrib.get("PartName", "").lstrip("/") in removed or any(
                k in " ".join(x.attrib.values()).lower()
                for k in ("external", "oleobject", "activex", "connection")
            ):
                ct.remove(x)
        ET.SubElement(
            ct,
            f"{{{C}}}Override",
            {
                "PartName": f"/{part}",
                "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
            },
        )
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename in removed:
                    continue
                data = zin.read(info.filename)
                if info.filename == "xl/workbook.xml":
                    data = ET.tostring(wb, encoding="utf-8", xml_declaration=True)
                elif info.filename == "xl/_rels/workbook.xml.rels":
                    data, rm = clean_rels(
                        ET.tostring(rel, encoding="utf-8", xml_declaration=True)
                    )
                    report["removed_relationships"] += [
                        {"part": info.filename, **x} for x in rm
                    ]
                elif info.filename == "[Content_Types].xml":
                    data = ET.tostring(ct, encoding="utf-8", xml_declaration=True)
                elif info.filename.endswith(".rels"):
                    data, rm = clean_rels(data)
                    report["removed_relationships"] += [
                        {"part": info.filename, **x} for x in rm
                    ]
                elif info.filename in old_by_part:
                    data = transform_sheet(
                        data,
                        RENAMES.get(
                            old_by_part[info.filename], old_by_part[info.filename]
                        ),
                        guards,
                        report,
                    )
                zout.writestr(info, data)
            zout.writestr(part, meta_xml())
    temp.replace(target)
    return report


def formula_rows(path):
    rows = []
    with zipfile.ZipFile(path) as z:
        _, ss = sheets(z)
        for s, p in ss:
            for c in ET.fromstring(z.read(p)).findall(".//m:c[m:f]", NS):
                f = c.find("m:f", NS)
                rows.append(
                    {
                        "sheet": s.attrib["name"],
                        "cell": c.attrib["r"],
                        "formula": f.text or "",
                        "shared_attributes": dict(sorted(f.attrib.items())),
                    }
                )
    rows.sort(key=lambda x: (x["sheet"], x["cell"]))
    raw = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return rows, hashlib.sha256(raw).hexdigest()


def scan(path):
    out = {
        "external_parts": [],
        "external_relationships": [],
        "external_content_types": [],
        "proprietary_formulas": [],
        "cached_errors": [],
        "direct_self_references": [],
    }
    with zipfile.ZipFile(path) as z:
        out["external_parts"] = [n for n in z.namelist() if n.startswith(PREFIXES)]
        for n in z.namelist():
            if n.endswith(".rels"):
                for x in ET.fromstring(z.read(n)):
                    if external_rel(x):
                        out["external_relationships"].append({"part": n, **x.attrib})
        for x in ET.fromstring(z.read("[Content_Types].xml")):
            if any(
                k in " ".join(x.attrib.values()).lower()
                for k in ("external", "oleobject", "activex", "connection")
            ):
                out["external_content_types"].append(x.attrib)
        _, ss = sheets(z)
        for s, p in ss:
            for c in ET.fromstring(z.read(p)).findall(".//m:c", NS):
                f = c.find("m:f", NS)
                v = c.find("m:v", NS)
                if f is not None and any(
                    q in (f.text or "").upper() for q in ("DBS(", "VIEW(", "SUBNM(")
                ):
                    out["proprietary_formulas"].append(
                        f"{s.attrib['name']}!{c.attrib['r']}"
                    )
                if f is not None:
                    ref = c.attrib["r"].replace("$", "")
                    text = re.sub(
                        r"(?:'[^']+'|[A-Za-z0-9_\u4e00-\u9fff ]+)!\$?[A-Z]{1,3}\$?\d+",
                        "",
                        f.text or "",
                    )
                    if ref in {
                        x.replace("$", "")
                        for x in re.findall(
                            r"(?<![!A-Z0-9_])(\$?[A-Z]{1,3}\$?\d+)", text
                        )
                    }:
                        out["direct_self_references"].append(
                            f"{s.attrib['name']}!{ref}"
                        )
                    for a, b in re.findall(
                        r"(?<![!A-Z0-9_])(\$?[A-Z]{1,3}\$?\d+):\$?([A-Z]{1,3}\$?\d+)",
                        text,
                    ):
                        ma, mb, mc = (
                            CRE.match(a.replace("$", "")),
                            CRE.match(b.replace("$", "")),
                            CRE.match(ref),
                        )
                        if (
                            ma
                            and mb
                            and mc
                            and min(col_index(ma.group(1)), col_index(mb.group(1)))
                            <= col_index(mc.group(1))
                            <= max(col_index(ma.group(1)), col_index(mb.group(1)))
                            and int(ma.group(2)) <= int(mc.group(2)) <= int(mb.group(2))
                        ):
                            out["direct_self_references"].append(
                                f"{s.attrib['name']}!{ref}"
                            )
                if c.attrib.get("t") == "e" or (v is not None and v.text in ERRORS):
                    out["cached_errors"].append(
                        {
                            "sheet": s.attrib["name"],
                            "cell": c.attrib["r"],
                            "value": v.text if v is not None else None,
                            "formula": f.text if f is not None else None,
                        }
                    )
    return out


def cell_value(root, ref, strings):
    c = next((x for x in root.findall(".//m:c", NS) if x.attrib.get("r") == ref), None)
    if c is None:
        return ""
    f = c.find("m:f", NS)
    if f is not None:
        return f.text or ""
    v = c.find("m:v", NS)
    if v is None:
        t = c.find("m:is/m:t", NS)
        return t.text if t is not None and t.text else ""
    return (
        strings[int(v.text)]
        if c.attrib.get("t") == "s" and v.text.isdigit()
        else (v.text or "")
    )


def col(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(name):
    result = 0
    for char in name:
        result = result * 26 + ord(char) - 64
    return result


def manifest(path):
    fr, fp = formula_rows(path)
    lookup = {(x["sheet"], x["cell"]): x for x in fr}
    out = {
        "template_version": "V1",
        "budget_year": 2026,
        "rule_version": "R1",
        "formula_manifest_hash": fp,
        "strict_formula_fingerprint": fp,
        "formula_count": len(fr),
        "system_ranges": ["SYS_META!A1:B6"],
        "reports": {},
        "sheets": [],
    }
    with zipfile.ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            strings = [
                "".join(t.text or "" for t in x.findall(".//m:t", NS))
                for x in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(
                    "m:si", NS
                )
            ]
        _, ss = sheets(z)
        roots = {}
        for s, p in ss:
            root = ET.fromstring(z.read(p))
            roots[s.attrib["name"]] = root
            d = root.find("m:dimension", NS)
            out["sheets"].append(
                {
                    "name": s.attrib["name"],
                    "state": s.attrib.get("state", "visible"),
                    "dimension": d.attrib.get("ref") if d is not None else None,
                }
            )
        for code, (sn, kind) in REPORTS.items():
            contract = REPORT_CONTRACTS[code]
            rows = contract["rows"]
            annual_column = contract["annual_column"]
            month_columns = contract["month_columns"]
            month_periods = [
                (column, f"{index + 1:02d}")
                for index, column in enumerate(month_columns)
            ]
            cols = (
                [(annual_column, "FY"), *month_periods]
                if kind == "total"
                else [*month_periods, (annual_column, "FY")]
            )
            label = contract["label_column"]
            ratio_row, ratio_parts = next(iter(contract["ratio"].items()))
            region = {
                "header_row": 21 if kind == "total" else 8,
                "month_columns": f"{month_columns[0]}:{month_columns[-1]}",
                "annual_column": annual_column,
                "rows": f"{rows.start}:{rows.stop - 1}",
                "label_column": label,
                "occ": {
                    "row": ratio_row,
                    "numerator_row": ratio_parts[0],
                    "denominator_row": ratio_parts[1],
                },
            }
            mapping = []
            for r in rows:
                lab = cell_value(roots[sn], f"{label}{r}", strings).strip()
                if (
                    not lab
                    or r in contract["exclude"]
                    or "平衡检查" in lab
                    or "不用填" in lab
                    or re.fullmatch(r"[+-]?\d+(?:\.\d+)?%?", lab)
                ):
                    continue
                if r in contract["ratio"]:
                    unit, aggregation = "RATIO", "RATIO"
                    parts = contract["ratio"][r]
                elif r in contract["derived"]:
                    unit, aggregation = "MONEY", "DERIVED"
                    parts = contract["derived"][r]
                elif r in contract["average"]:
                    unit, aggregation, parts = "COUNT", "AVERAGE", None
                elif r in contract["count_sum"]:
                    unit, aggregation, parts = "COUNT", "SUM", None
                else:
                    unit, aggregation, parts = "MONEY", "SUM", None
                for c, period in cols:
                    ref = f"{c}{r}"
                    f = lookup.get((sn, ref))
                    item = {
                        "row_code": f"R{r:04d}",
                        "row_label": lab,
                        "period": period,
                        "cell": ref,
                        "classification": "formula" if f else "input",
                        "formula": f["formula"] if f else None,
                        "shared_attributes": f["shared_attributes"] if f else {},
                        "allowed_functions": sorted(
                            {
                                match.group(1).upper()
                                for match in FRE.finditer(f["formula"] if f else "")
                            }
                        ),
                        "unit": unit,
                        "aggregation": aggregation,
                        "source": {"sheet": sn, "cell": ref},
                    }
                    if parts:
                        item["numerator_cell"] = f"{c}{parts[0]}"
                        item["denominator_cell"] = f"{c}{parts[1]}"
                    mapping.append(item)
            out["reports"][code] = {
                "sheet": sn,
                "region": region,
                "mapped_cell_count": len(mapping),
                "mapping": mapping,
            }
    out["mapped_cell_count"] = sum(
        x["mapped_cell_count"] for x in out["reports"].values()
    )
    return out


def lo(path, label):
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise RuntimeError("LibreOffice unavailable")
    root = Path(tempfile.mkdtemp(prefix=f"template-{label}-"))
    (root / "in").mkdir()
    (root / "out").mkdir()
    (root / "profile").mkdir()
    src = root / "in" / f"{label}.xlsx"
    shutil.copy2(path, src)
    cmd = [
        exe,
        f"-env:UserInstallation=file://{root / 'profile'}",
        "--headless",
        "--convert-to",
        "xlsx",
        "--outdir",
        str(root / "out"),
        str(src),
    ]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    dest = root / "out" / src.name
    if run.returncode or not dest.exists():
        raise RuntimeError(f"LibreOffice failed: {run.stdout} {run.stderr}")
    return dest, {
        "command": cmd,
        "returncode": run.returncode,
        "stdout": run.stdout.strip(),
        "stderr": run.stderr.strip(),
        "private_profile": str(root / "profile"),
    }


def excel(path):
    q = [
        "osascript",
        "-e",
        'tell application "Microsoft Excel" to get name of every workbook',
    ]
    before = subprocess.run(q, capture_output=True, text=True, timeout=30)
    script = """on run argv\nset targetPath to item 1 of argv\nset targetName to item 2 of argv\nwith timeout of 600 seconds\ntell application "Microsoft Excel"\nopen workbook workbook file name targetPath\nset targetBook to workbook targetName\nrepeat with targetSheet in worksheets of targetBook\nactivate object targetSheet\ncalculate sheet\nend repeat\nsave targetBook\nclose targetBook saving yes\nend tell\nend timeout\nend run"""
    run = subprocess.run(
        ["osascript", "-e", script, str(path), path.name],
        capture_output=True,
        text=True,
        timeout=700,
    )
    after = subprocess.run(q, capture_output=True, text=True, timeout=30)
    return {
        "returncode": run.returncode,
        "stdout": run.stdout.strip(),
        "stderr": run.stderr.strip(),
        "workbooks_before": before.stdout.strip(),
        "workbooks_after": after.stdout.strip(),
        "user_workbooks_preserved": before.returncode == after.returncode == 0
        and before.stdout.strip() == after.stdout.strip(),
    }


def cached(path, mf):
    wanted = {
        (x["sheet"], i["cell"]) for x in mf["reports"].values() for i in x["mapping"]
    }
    out = {}
    with zipfile.ZipFile(path) as z:
        _, ss = sheets(z)
        for s, p in ss:
            sn = s.attrib["name"]
            for c in ET.fromstring(z.read(p)).findall(".//m:c", NS):
                key = (sn, c.attrib.get("r"))
                if key in wanted:
                    v = c.find("m:v", NS)
                    out[key] = v.text if v is not None else None
    return out


def compare(a, b, mf):
    av, bv = cached(a, mf), cached(b, mf)
    diff = []
    total = numeric = occ = 0
    for report in mf["reports"].values():
        for item in report["mapping"]:
            key = (report["sheet"], item["cell"])
            x, y = av.get(key), bv.get(key)
            total += 1
            try:
                xd = Decimal(x) if x not in (None, "") else Decimal(0)
                yd = Decimal(y) if y not in (None, "") else Decimal(0)
            except InvalidOperation:
                if x != y:
                    diff.append(
                        {"sheet": key[0], "cell": key[1], "excel": x, "libreoffice": y}
                    )
                    continue
            numeric += 1
            prec = Decimal("0.0001") if item["unit"] == "RATIO" else Decimal("0.01")
            occ += item["unit"] == "RATIO"
            if xd.quantize(prec, rounding=ROUND_HALF_UP) != yd.quantize(
                prec, rounding=ROUND_HALF_UP
            ):
                diff.append(
                    {
                        "sheet": key[0],
                        "cell": key[1],
                        "excel": x,
                        "libreoffice": y,
                        "unit": item["unit"],
                    }
                )
    return {
        "mapped_cells_compared": total,
        "numeric_cells_compared": numeric,
        "occ_cells_compared": occ,
        "difference_count": len(diff),
        "differences": diff[:200],
    }


def main():
    art = ROOT / "artifacts"
    ev = ROOT / ".omo/evidence/template-release"
    ev.mkdir(parents=True, exist_ok=True)
    target = art / "平台标准预算模板_V1.xlsx"
    before = sha(SOURCE)
    if before != EXPECTED:
        raise SystemExit(f"source SHA mismatch {before}")
    raw = build(SOURCE, target)
    rawlo, rawrun = lo(target, "raw")
    rawscan = scan(rawlo)
    guards = {
        (x["sheet"], x["cell"]): x.get("formula") or ""
        for x in rawscan["cached_errors"]
        if x.get("formula")
    }
    if len(guards) != len(rawscan["cached_errors"]):
        raise RuntimeError("LibreOffice returned non-formula errors")
    final = build(SOURCE, target, guards)
    mf = manifest(target)
    write(art / "template_manifest.json", mf)
    write(art / "template_manifest_V1.json", mf)
    pub = scan(target)
    final_lo, finalrun = lo(target, "final")
    loscan = scan(final_lo)
    excopy = (
        Path(tempfile.mkdtemp(prefix="template-excel-golden-")) / "excel_golden.xlsx"
    )
    shutil.copy2(target, excopy)
    exrun = excel(excopy)
    exscan = scan(excopy) if exrun["returncode"] == 0 else {"cached_errors": []}
    cmp = compare(excopy, final_lo, mf) if exrun["returncode"] == 0 else None
    rows, fp = formula_rows(target)
    after = sha(SOURCE)
    block = []
    if before != after or after != EXPECTED:
        block.append("immutable source SHA changed")
    if fp != mf["strict_formula_fingerprint"]:
        block.append("formula fingerprint mismatch")
    for k in pub:
        if pub[k]:
            block.append(f"published template contains {k}")
    if loscan["cached_errors"]:
        block.append("LibreOffice cached errors remain")
    if exrun["returncode"]:
        block.append(
            "Excel target-only AppleScript could not open/access the target workbook; the existing user workbook was preserved"
        )
    elif not exrun["user_workbooks_preserved"]:
        block.append("Excel user workbook set changed")
    elif exscan["cached_errors"]:
        block.append("Excel golden cached errors remain")
    elif cmp and cmp["difference_count"]:
        block.append("Excel/LibreOffice mapped values differ")
    status = "PASS" if not block else "BLOCKED"
    gold = {
        "status": status,
        "release_blockers": block,
        "source_sha256_before": before,
        "source_sha256_after": after,
        "libreoffice": {
            "raw_error_count": len(rawscan["cached_errors"]),
            "final_error_count": len(loscan["cached_errors"]),
            "raw_run": rawrun,
            "final_run": finalrun,
        },
        "excel": {**exrun, "cached_error_count": len(exscan["cached_errors"])},
        "comparison": cmp,
    }
    write(art / "黄金样本报告.json", gold)
    report = {
        "release_status": status,
        "release_blockers": block,
        "source_sha256_before": before,
        "source_sha256_after": after,
        "target_sha256": sha(target),
        "build_method": "OOXML/ZIP; no openpyxl workbook save",
        "sheet_renames": RENAMES,
        "raw_build": raw,
        "final_build": final,
        "formula_error_whitelist_count": len(guards),
        "formula_error_whitelist": [
            {"sheet": s, "cell": c, "formula": f}
            for (s, c), f in sorted(guards.items())
        ],
        "raw_libreoffice_error_count": len(rawscan["cached_errors"]),
        "final_libreoffice_error_count": len(loscan["cached_errors"]),
        "published_cached_error_count": len(pub["cached_errors"]),
        "formula_count": len(rows),
        "strict_formula_fingerprint": fp,
        "manifest_formula_fingerprint": mf["strict_formula_fingerprint"],
        "mapped_cell_count": mf["mapped_cell_count"],
        "package_scan": pub,
        "golden_sample_status": status,
    }
    write(art / "template_purification_report.json", report)
    summary = {
        "scenario": "V1 template release rebuild and dual-engine verification",
        "invocation": "python3 scripts/purify_template.py",
        "observable": {
            "release_status": status,
            "source_sha256_unchanged": before == after == EXPECTED,
            "target_sha256": sha(target),
            "proprietary_formulas": len(pub["proprietary_formulas"]),
            "external_parts": len(pub["external_parts"]),
            "external_relationships": len(pub["external_relationships"]),
            "published_cached_errors": len(pub["cached_errors"]),
            "libreoffice_cached_errors": len(loscan["cached_errors"]),
            "formula_fingerprint_match": fp == mf["strict_formula_fingerprint"],
            "mapped_cell_count": mf["mapped_cell_count"],
            "excel_difference_count": cmp["difference_count"] if cmp else None,
        },
        "artifacts": [
            str(target),
            str(art / "template_manifest.json"),
            str(art / "template_manifest_V1.json"),
            str(art / "template_purification_report.json"),
            str(art / "黄金样本报告.json"),
        ],
    }
    write(ev / "verification_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(2 if block else 0)


if __name__ == "__main__":
    main()
