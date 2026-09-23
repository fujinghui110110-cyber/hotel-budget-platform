"""Exercise real isolated Python runtimes and SQLite; no production paths touched."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from scripts.release_manager import ReleaseManager, atomic_json


def main():
    with tempfile.TemporaryDirectory(prefix='预算 发布 空格 ') as directory:
        root = Path(directory)
        data = root / 'data'
        data.mkdir()
        db = data / 'db.sqlite3'
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE upload(id INTEGER PRIMARY KEY, original TEXT)')
            conn.execute("INSERT INTO upload(original) VALUES ('before')")
        roots = {}
        for label in ('storage', 'templates', 'config'):
            roots[label] = data / label
            roots[label].mkdir()
            (roots[label] / 'original').write_text(label)
        manager = ReleaseManager(root, data)
        releases = []
        for index in (1, 2):
            release = root / 'releases' / f'2026.09.16.{index}'
            release.mkdir(parents=True)
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(release / '.venv')], check=True)
            atomic_json(release / 'manifest.json', {
                'schema': 2, 'version': release.name, 'commit_sha': '1234567890abcdef1234567890abcdef12345678',
                'repository': 'fujinghui110110-cyber/hotel-budget-platform', 'authenticity': 'github-attestation',
                'database_schema_read_range': [1, 2], 'database_schema_write_range': [1, 2],
                'capabilities': ['annual-targets-v1', 'locked-history-v1'],
                'migration_ids': ['0001_fixture'], 'supported_python': [f'{sys.version_info.major}.{sys.version_info.minor}'],
                'rollback': {'mode': 'code_only_when_compatible'}})
            atomic_json(release / 'runtime-validated.json', {'stdlib_runtime': True})
            releases.append(release)
        atomic_json(manager.pointer, manager.pointer_for(releases[0]))
        def health(pointer):
            subprocess.run([pointer['python'], '-c', 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); assert c.execute("select count(*) from upload").fetchone()[0] > 0', str(db)], check=True)
        def migrate(pointer, database):
            subprocess.run([pointer['python'], '-c', 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("CREATE TABLE IF NOT EXISTS new_schema(id INTEGER)"); c.commit()', str(database)], check=True)
        def reconcile(before, after):
            with sqlite3.connect(before) as a, sqlite3.connect(after) as b:
                assert a.execute('select * from upload').fetchall() == b.execute('select * from upload').fetchall()
        manager.install_release(releases[1], database=db, roots=roots, schema=1, required_capabilities=['locked-history-v1'],
                                quiesce=lambda: None, migrate=migrate, reconcile=reconcile, healthcheck=health,
                                verify=lambda: None)  # Local fixture only; no claim of signed release acceptance.
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO upload(original) VALUES ('after upgrade')")
        manager.rollback_code(releases[0], schema=2, required_capabilities=['locked-history-v1'], quiesce=lambda: None, healthcheck=health)
        with sqlite3.connect(db) as conn:
            assert conn.execute('select count(*) from upload').fetchone()[0] == 2
        for label in roots:
            assert (data / 'backups' / releases[1].name / label / 'original').read_text() == label
        print(json.dumps({'result': 'PASS', 'runtime': sys.platform, 'independent_venvs': 2, 'uploads_after_upgrade_and_rollback': 2,
                          'production_data_touched': False, 'authentic_release_verification': 'NOT_RUN'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
