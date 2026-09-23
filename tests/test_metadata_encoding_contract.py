"""Encoding variants must preserve template identity without bypassing signatures."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from django.core import signing
from django.test import SimpleTestCase
from openpyxl import Workbook

from budgeting.excel.ooxml import formula_manifest, read_sys_meta, validate_upload_contract
from budgeting.services.template_delivery import _inject_sys_meta

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


class MetadataEncodingContractTests(SimpleTestCase):
    def test_legal_encodings_keep_signature_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / 'template.xlsx'
            workbook = Workbook()
            workbook.remove(workbook.active)
            for name in ('酒店损益总表（含名酒）', '酒店损益总表（不含名酒）',
                         '损益表（含名酒）（拆中智）', '损益表（不含名酒）（拆中智）', 'SYS_META'):
                workbook.create_sheet(name)
            workbook.save(template)
            workbook.close()
            _, digest = formula_manifest(template)
            payload = dict(project_code='ENC', budget_year=2027, cycle_id=1, template_version='encoding-test')
            meta = dict(payload, rule_version='R1', formula_manifest_hash=digest,
                        project_signature=signing.dumps(payload, salt='budget-template-v1'))
            signed = root / 'signed.xlsx'
            _inject_sys_meta(template, signed, meta, input_cells={})
            manifest = root / 'manifest.json'
            manifest.write_text('{}')
            upload = SimpleNamespace(project=SimpleNamespace(code='ENC'), cycle=SimpleNamespace(budget_year=2027),
                cycle_id=1, template=SimpleNamespace(version='encoding-test', rule_version='R1',
                formula_manifest_hash=digest, file_path=str(template), manifest_path=str(manifest)))
            with zipfile.ZipFile(signed) as archive:
                parts = {name: archive.read(name) for name in archive.namelist()}
            metadata_name = 'xl/worksheets/sheet5.xml'
            for mode in ('inline', 'rich', 'shared'):
                with self.subTest(mode=mode):
                    sheet = ET.fromstring(parts[metadata_name])
                    shared = ET.Element(f'{{{NS}}}sst')
                    texts = []
                    for cell in sheet.findall(f'.//{{{NS}}}c'):
                        if cell.get('t') != 'inlineStr':
                            continue
                        text = ''.join(n.text or '' for n in cell.findall(f'.//{{{NS}}}t'))
                        texts.append(text)
                        for child in list(cell):
                            cell.remove(child)
                        if mode == 'shared':
                            cell.set('t', 's')
                            ET.SubElement(cell, f'{{{NS}}}v').text = str(len(texts) - 1)
                            container = ET.SubElement(shared, f'{{{NS}}}si')
                        else:
                            container = ET.SubElement(cell, f'{{{NS}}}is')
                        if mode in ('rich', 'shared'):
                            for chunk in (text[:len(text)//2], text[len(text)//2:]):
                                ET.SubElement(ET.SubElement(container, f'{{{NS}}}r'), f'{{{NS}}}t').text = chunk
                        else:
                            ET.SubElement(container, f'{{{NS}}}t').text = text
                        if text == meta['project_signature']:
                            signature_tail = container.findall(f'.//{{{NS}}}t')[-1]
                    variant = root / f'{mode}.xlsx'
                    converted = dict(parts)
                    converted[metadata_name] = ET.tostring(sheet)
                    if mode == 'shared':
                        converted['xl/sharedStrings.xml'] = ET.tostring(shared)
                        types = ET.fromstring(converted['[Content_Types].xml'])
                        ET.SubElement(types, '{http://schemas.openxmlformats.org/package/2006/content-types}Override',
                            PartName='/xl/sharedStrings.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml')
                        converted['[Content_Types].xml'] = ET.tostring(types)
                        relationships = ET.fromstring(converted['xl/_rels/workbook.xml.rels'])
                        ET.SubElement(relationships, '{http://schemas.openxmlformats.org/package/2006/relationships}Relationship',
                            Id='rIdSharedStrings', Target='sharedStrings.xml',
                            Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings')
                        converted['xl/_rels/workbook.xml.rels'] = ET.tostring(relationships)
                    with zipfile.ZipFile(variant, 'w') as archive:
                        for name, content in converted.items():
                            archive.writestr(name, content)
                    self.assertEqual(read_sys_meta(variant), read_sys_meta(signed))
                    issues = validate_upload_contract(upload, variant)
                    self.assertFalse([issue for issue in issues if issue[0] == 'P0'], issues)
                    upload.project.code = 'OTHER'
                    try:
                        bad = validate_upload_contract(upload, variant)
                        self.assertIn('PROJECT_SIGNATURE', {issue[1] for issue in bad})
                    finally:
                        upload.project.code = 'ENC'
                    signature_tail.text = (signature_tail.text or '') + 'X'
                    converted['xl/sharedStrings.xml' if mode == 'shared' else metadata_name] = ET.tostring(shared if mode == 'shared' else sheet)
                    tampered = root / f'{mode}-bad-signature.xlsx'
                    with zipfile.ZipFile(tampered, 'w') as archive:
                        for name, content in converted.items():
                            archive.writestr(name, content)
                    self.assertIn('PROJECT_SIGNATURE', {issue[1] for issue in validate_upload_contract(upload, tampered)})
