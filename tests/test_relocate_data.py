import importlib.util
from pathlib import Path
import sqlite3
import tempfile
from unittest import TestCase

SPEC = importlib.util.spec_from_file_location('relocate_data', Path(__file__).resolve().parents[1] / 'scripts/relocate_data.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class RelocateDataTests(TestCase):
    def test_dry_run_then_backup_and_apply_only_existing_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            template = root / 'artifacts/v3/2027/template.xlsx'
            template.parent.mkdir(parents=True)
            template.write_bytes(b'original workbook unchanged')
            uploaded = root / 'storage/uploads/original.xlsx'
            uploaded.parent.mkdir(parents=True)
            uploaded.write_bytes(b'uploaded workbook')
            database = root / 'db.sqlite3'
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE budgeting_templateversion(id INTEGER, file_path TEXT, manifest_path TEXT)')
                connection.execute('INSERT INTO budgeting_templateversion VALUES (1, ?, ?)', ('/old/project/artifacts/v3/2027/template.xlsx', '/old/project/artifacts/missing.json'))
                connection.execute('CREATE TABLE budgeting_specialindicatorbatch(id INTEGER, original TEXT)')
                connection.execute('INSERT INTO budgeting_specialindicatorbatch VALUES (1, ?)', ('/old/project/storage/uploads/original.xlsx',))
            before = database.read_bytes()
            preview = module.relocate(database, '/old/project', root)
            self.assertEqual(preview['planned'], 2)
            self.assertEqual(preview['unresolved'], 1)
            self.assertEqual(database.read_bytes(), before)
            report = module.relocate(database, '/old/project', root, apply=True)
            self.assertEqual(report['updated'], 2)
            self.assertTrue(Path(report['backup']).is_file())
            with sqlite3.connect(report['backup']) as connection:
                self.assertEqual(connection.execute('SELECT file_path FROM budgeting_templateversion').fetchone()[0], '/old/project/artifacts/v3/2027/template.xlsx')
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute('SELECT file_path FROM budgeting_templateversion').fetchone()[0], str(template))
                self.assertEqual(connection.execute('SELECT original FROM budgeting_specialindicatorbatch').fetchone()[0], 'uploads/original.xlsx')
                self.assertEqual(connection.execute('SELECT manifest_path FROM budgeting_templateversion').fetchone()[0], '/old/project/artifacts/missing.json')
            self.assertEqual(template.read_bytes(), b'original workbook unchanged')

    def test_windows_old_root_and_external_path(self):
        root = Path('/new/project')
        value, target, state = module.plan_reference('C:\\Budget\\storage\\a.xlsx', 'C:\\Budget', root, 'storage')
        self.assertEqual(value, 'a.xlsx')
        self.assertEqual(state, 'relocate')
        _, target, state = module.plan_reference('/other/private.xlsx', '/old/project', root, 'root')
        self.assertIsNone(target)
        self.assertEqual(state, 'outside_old_root')
