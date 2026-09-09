import json
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

from django.conf import settings

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'


def _contents(path):
    with zipfile.ZipFile(path) as package:
        shared = []
        if 'xl/sharedStrings.xml' in package.namelist():
            shared = [''.join(node.itertext()) for node in ET.fromstring(package.read('xl/sharedStrings.xml'))]
        relations = {r.attrib['Id']: r.attrib['Target'] for r in ET.fromstring(package.read('xl/_rels/workbook.xml.rels'))}
        workbook = ET.fromstring(package.read('xl/workbook.xml'))
        sheets, values = [], {}
        for sheet in workbook.findall('m:sheets/m:sheet', NS):
            name = sheet.attrib['name']
            sheets.append((name, sheet.attrib.get('state', 'visible')))
            target = relations[sheet.attrib[REL]].lstrip('/')
            if not target.startswith('xl/'):
                target = 'xl/' + target
            root = ET.fromstring(package.read(target))
            for cell in root.findall('.//m:sheetData/m:row/m:c', NS):
                if cell.find('m:f', NS) is not None:
                    continue
                raw = cell.findtext('m:v', '', NS)
                kind = cell.attrib.get('t', 'n')
                if kind == 'inlineStr':
                    value = ''.join(t.text or '' for t in cell.findall('.//m:t', NS))
                elif kind == 's':
                    value = shared[int(raw)] if raw else ''
                elif kind == 'n' and raw:
                    try:
                        value = Decimal(raw)
                    except InvalidOperation:
                        value = raw
                else:
                    value = raw
                if value != '':
                    values[(name, cell.attrib['r'])] = value
        return sheets, values


def validate_v2_structure(upload, path):
    manifest_path = Path(upload.template.manifest_path)
    if not manifest_path.is_absolute():
        manifest_path = settings.BASE_DIR / manifest_path
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not manifest.get('management_v2'):
        return []
    source = Path(upload.template.file_path)
    if not source.is_absolute():
        source = settings.BASE_DIR / source
    expected_sheets, expected = _contents(source)
    sheets, values = _contents(path)
    issues = []
    report_sheets = {report['sheet'] for report in manifest.get('reports', {}).values() if report.get('sheet')}
    protected = report_sheets | {'SYS_META'} if report_sheets else {name for name, _ in expected_sheets}
    actual_sheets = dict(sheets)
    if any(actual_sheets.get(name) != state for name, state in expected_sheets if name in protected):
        issues.append(('P0', 'V2_SHEET_STRUCTURE', '汇总工作表名称或可见性与签名模板不一致。'))
    editable = {(sheet, ref) for sheet, refs in manifest.get('input_cells', {}).items() for ref in refs}
    editable.update(('SYS_META', f'B{row}') for row in range(1, 7))
    for key in sorted(set(expected) | set(values)):
        if key[0] in protected and key not in editable and values.get(key, '') != expected.get(key, ''):
            issues.append(('P0', 'V2_SYSTEM_CELL_CHANGED', '非填报单元格发生变化。', '!'.join(key)))
            if len(issues) >= 30:
                break
    return issues
