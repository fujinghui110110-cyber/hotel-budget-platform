"""Relocate known file references in a copied SQLite database; dry-run by default."""
import argparse
from datetime import datetime
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
# Relative paths are interpreted according to the application's existing readers.
FIELDS = {
    'budgeting_templateversion': {'file_path': 'root', 'manifest_path': 'root'},
    'budgeting_uploadversion': {'original_path': 'storage', 'recalculated_path': 'storage'},
    'budgeting_freezesnapshot': {'directory': 'storage', 'manifest_path': 'storage'},
    'budgeting_snapshotartifact': {'relative_path': 'storage'},
    'budgeting_specialindicatorbatch': {'original': 'storage'},
}


def old_path(value):
    return PureWindowsPath(value) if PureWindowsPath(value).drive or '\\' in value else PurePosixPath(value)


def plan_reference(value, old_root, new_root, base):
    path = old_path(value)
    if not path.is_absolute():
        target = new_root / ('storage' if base == 'storage' else '') / Path(*path.parts)
        return value, target, 'relative'
    try:
        relative = path.relative_to(old_path(old_root))
    except ValueError:
        return value, None, 'outside_old_root'
    if '..' in relative.parts:
        return value, None, 'unsafe_path'
    target = new_root.joinpath(*relative.parts)
    if base == 'storage':
        try:
            updated = target.relative_to(new_root / 'storage').as_posix()
        except ValueError:
            return value, target, 'outside_storage'
    else:
        updated = str(target)
    return updated, target, 'relocate'


def relocate(database, old_root, new_root, apply=False):
    database, new_root = Path(database).resolve(), Path(new_root).resolve()
    if not database.is_file():
        raise ValueError('数据库不存在，未创建空数据库。')
    if not old_path(old_root).is_absolute():
        raise ValueError('--old-root 必须为旧项目的绝对路径。')
    report = {'mode': 'apply' if apply else 'dry-run', 'database': str(database),
              'new_root': str(new_root), 'backup': None, 'updated': 0, 'items': []}
    connection = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True)
    try:
        changes = []
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, fields in FIELDS.items():
            if table not in tables:
                continue
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for field, base in fields.items():
                if field not in columns:
                    continue
                for pk, value in connection.execute(f'SELECT id, "{field}" FROM "{table}" WHERE "{field}" IS NOT NULL AND "{field}" != \'\''):
                    updated, target, state = plan_reference(value, old_root, new_root, base)
                    if target is not None and not target.exists():
                        state = 'missing'
                    item = {'table': table, 'id': str(pk), 'field': field, 'old': value,
                            'new': updated, 'status': state}
                    report['items'].append(item)
                    if state == 'relocate' and updated != value:
                        changes.append((table, field, pk, updated))
        report['planned'] = len(changes)
        if apply and changes:
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            backup = database.with_name(database.name + '.before-relocate-' + stamp + '.bak')
            with sqlite3.connect(backup) as backup_connection:
                connection.backup(backup_connection)
            backup.chmod(0o600)
            report['backup'] = str(backup)
            with connection:
                for table, field, pk, updated in changes:
                    connection.execute(f'UPDATE "{table}" SET "{field}" = ? WHERE id = ?', (updated, pk))
            report['updated'] = len(changes)
        report['unresolved'] = sum(item['status'] not in {'relative', 'relocate'} for item in report['items'])
        return report
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description='迁移数据库中已知文件路径，默认仅预览；原始预算文件不改写')
    parser.add_argument('--old-root', required=True)
    parser.add_argument('--new-root', type=Path, default=ROOT)
    parser.add_argument('--database', type=Path, help='默认新项目目录下 db.sqlite3')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', type=Path, help='另存JSON报告，文件已存在时拒绝覆盖')
    args = parser.parse_args()
    if args.report and args.report.exists():
        parser.error('报告文件已存在，请换一个文件名。')
    result = relocate(args.database or args.new_root / 'db.sqlite3', args.old_root, args.new_root, args.apply)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        with args.report.open('x', encoding='utf-8') as handle:
            handle.write(output + '\n')
    print(output)
    return 2 if result['unresolved'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f'迁移失败：{exc}', file=sys.stderr)
        sys.exit(1)
