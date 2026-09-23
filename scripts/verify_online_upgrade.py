#!/usr/bin/env python3
"""Run an isolated old-to-bridge-to-modern online upgrade rehearsal.

The database migration, backup, release staging, health checks, and launcher
are the production implementations.  Only the release transport is served by
the local HTTP fixture.  A provenance fixture is available for CI packages
which do not yet have a GitHub attestation; its use is recorded in the JSON
report and never presented as a real attestation.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from contextlib import closing
import hashlib
import http.cookiejar
import http.server
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "48a933393e87f04d742c607dc9d955f5bb4e9531"
OLD_VERSION = "2026.09.14.1"
BRIDGE_VERSION = "2026.09.23.0"
MODERN_VERSION = "2026.09.23.1"
REPOSITORY = "fujinghui110110-cyber/hotel-budget-platform"
ASSET_NAME = "budget-system-update.zip"
MAX_ARCHIVE = 100 * 1024 * 1024

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class VerificationError(RuntimeError):
    """A concise, user-facing verification failure."""


def _json_default(value):
    if isinstance(value, bytes):
        return {"bytes_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as exc:
        detail = exc.output.decode("utf-8", "replace")[-1000:]
        raise VerificationError(f"git {args[0]} 失败：{detail}") from exc


def _safe_extract_git_archive(repo: Path, commit: str, destination: Path):
    """Extract a Git tree without allowing archive members to escape."""
    archive = _git(repo, "archive", "--format=tar", commit)
    if destination.exists():
        if any(destination.iterdir()):
            raise VerificationError("旧版安装目录必须为空。")
    else:
        destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        root = destination.resolve()
        for member in source.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(root):
                raise VerificationError("旧版 Git 归档包含越界路径。")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise VerificationError("旧版 Git 归档包含不支持的链接或特殊文件。")
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(member)
            if stream is None:
                raise VerificationError("无法读取旧版 Git 归档文件。")
            target.write_bytes(stream.read())
            if member.mode & 0o111:
                target.chmod(target.stat().st_mode | 0o111)


def _zip_manifest(path: Path) -> tuple[dict, bytes]:
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"发布包不存在或不安全：{path}")
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_ARCHIVE:
        raise VerificationError(f"发布包大小不在允许范围：{path}")
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip():
                raise VerificationError(f"发布包完整性校验失败：{path}")
            manifest = json.loads(archive.read("manifest.json"))
            files = manifest.get("files", {})
            names = [item.filename for item in archive.infolist()]
            if len(names) != len({name.casefold() for name in names}):
                raise VerificationError(f"发布包存在重复路径：{path}")
            if set(names) != {"manifest.json", *files}:
                raise VerificationError(f"发布包清单与文件不一致：{path}")
            for name, digest in files.items():
                data = archive.read(name)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise VerificationError(f"发布包文件 hash 不一致：{name}")
    except (KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise VerificationError(f"无法读取发布包清单：{path}") from exc
    return manifest, path.read_bytes()


def _validate_inputs(bridge: Path, modern: Path):
    bridge_manifest, bridge_bytes = _zip_manifest(bridge)
    if bridge_manifest.get("schema") != 1 or bridge_manifest.get("version") != BRIDGE_VERSION:
        raise VerificationError("bridge 必须是 schema 1 的 2026.09.23.0。")
    if bridge_manifest.get("bridge_only") is not True or bridge_manifest.get("base_commit") != BASE_COMMIT:
        raise VerificationError("bridge 缺少旧版基线或 bridge_only 标记。")
    if bridge_manifest.get("database_migrations_changed") is not False:
        raise VerificationError("bridge 不得携带数据库迁移变化。")
    modern_manifest, modern_bytes = _zip_manifest(modern)
    if modern_manifest.get("schema") != 2 or modern_manifest.get("version") != MODERN_VERSION:
        raise VerificationError("modern 必须是 schema 2 的 2026.09.23.1。")
    if modern_manifest.get("authenticity") != "github-attestation":
        raise VerificationError("modern 清单必须保留 github-attestation 声明；fixture 只替换验证传输。")
    if "requirements-update.lock" not in modern_manifest.get("files", {}):
        raise VerificationError("modern 缺少哈希锁定依赖清单。")
    return {
        "bridge": {
            "version": bridge_manifest["version"],
            "schema": bridge_manifest["schema"],
            "sha256": hashlib.sha256(bridge_bytes).hexdigest(),
            "size": len(bridge_bytes),
            "commit_sha": bridge_manifest.get("commit_sha", ""),
        },
        "modern": {
            "version": modern_manifest["version"],
            "schema": modern_manifest["schema"],
            "sha256": hashlib.sha256(modern_bytes).hexdigest(),
            "size": len(modern_bytes),
            "commit_sha": modern_manifest.get("commit_sha", ""),
        },
    }


@dataclass(frozen=True)
class Artifact:
    artifact_id: int
    version: str
    archive: Path
    data: bytes
    manifest: dict

    @property
    def release(self):
        return {
            "tag_name": "v" + self.version,
            "name": "预算系统 " + self.version,
            "body": "isolated online-upgrade rehearsal fixture",
            "draft": False,
            "prerelease": False,
            "assets": [{
                "name": ASSET_NAME,
                "id": self.artifact_id,
                "size": len(self.data),
                "digest": "sha256:" + hashlib.sha256(self.data).hexdigest(),
                "browser_download_url": "fixture://" + str(self.artifact_id),
            }],
        }


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    server_version = "BudgetUpgradeFixture/1"

    def _send(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self._handle()

    def do_GET(self):
        self._handle()

    def _handle(self):
        fixture: "FixtureServer" = getattr(self.server, "fixture")
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/")
        if path.endswith("/releases/latest"):
            self._send(200, "application/vnd.github+json", json.dumps(fixture.bridge.release).encode())
            return
        if path.endswith("/releases"):
            payload = [fixture.modern.release, fixture.bridge.release]
            self._send(200, "application/vnd.github+json", json.dumps(payload).encode())
            return
        match = re.search(r"/releases/assets/(\d+)$", path)
        if match:
            artifact = fixture.artifacts.get(int(match.group(1)))
            if artifact is None:
                self._send(404, "text/plain; charset=utf-8", b"not found")
            else:
                self._send(200, "application/octet-stream", artifact.data)
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")

    def log_message(self, *_args):
        return


class FixtureServer:
    """A local GitHub-shaped transport; no database or launcher is mocked."""

    def __init__(self, bridge: Artifact, modern: Artifact):
        self.bridge = bridge
        self.modern = modern
        self.artifacts = {bridge.artifact_id: bridge, modern.artifact_id: modern}
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        setattr(self.httpd, "fixture", self)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="budget-upgrade-fixture", daemon=True)

    @property
    def api(self):
        return f"http://127.0.0.1:{self.httpd.server_port}/api/repos/{REPOSITORY}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _artifact(artifact_id: int, path: Path, manifest: dict) -> Artifact:
    return Artifact(artifact_id, manifest["version"], path, path.read_bytes(), manifest)


def _choose_port(requested: int | None) -> int:
    if requested is not None:
        if not 1024 <= requested <= 65535:
            raise VerificationError("端口必须在 1024-65535 之间。")
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _environment(install: Path, data: Path, port: int) -> dict[str, str]:
    storage = data / "storage"
    templates = data / "templates"
    runtime = install / ".runtime"
    logs = install / "logs"
    for path in (storage, templates, runtime, logs):
        path.mkdir(parents=True, exist_ok=True)
    return {
        **{key: value for key, value in os.environ.items()
           if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}},
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_DEBUG": "1",
        "DJANGO_SECRET_KEY": "online-upgrade-fixture-secret-" + "x" * 48,
        "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "DATABASE_PATH": str(data / "budget.sqlite3"),
        "BUDGET_STORAGE_ROOT": str(storage),
        "BUDGET_TEMPLATE_ROOT": str(templates),
        "BUDGET_RUNTIME_ROOT": str(runtime),
        "BUDGET_LOG_ROOT": str(logs),
        "BUDGET_INSTALL_ROOT": str(install),
        "LOGIN_RATE_LIMIT_PATH": str(data / "login-rate-limit.sqlite3"),
        "BUDGET_PROCESS_UPLOAD_INLINE": "0",
        "PUBLIC_ACCESS": "0",
        "SOFFICE_BIN": sys.executable,
        "PORT": str(port),
        "UPGRADE_FIXTURE_PUBLIC_PORT": str(_choose_port(None)),
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8:replace",
    }


def _run(command, *, cwd: Path, env: dict[str, str], timeout: int, label: str):
    result = subprocess.run(
        [str(item) for item in command], cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if result.returncode:
        detail = (result.stdout + "\n" + result.stderr).strip()[-5000:]
        raise VerificationError(f"{label} 失败（退出码 {result.returncode}）：\n{detail}")
    return result


def _manage(python: Path, install: Path, env: dict[str, str], *args: str, timeout=300):
    return _run([python, install / "manage.py", *args], cwd=install, env=env, timeout=timeout, label="manage.py " + " ".join(args))


def _prepare_sentinel(python: Path, install: Path, data: Path, env: dict[str, str]):
    storage = data / "storage"
    templates = data / "templates"
    upload = storage / "sentinel-upload.xlsx"
    template = templates / "sentinel-template.xlsx"
    catalog = templates / "sentinel-template.json"
    upload.write_bytes(b"online-upgrade-upload-sentinel-v1\n")
    template.write_bytes(b"online-upgrade-template-sentinel-v1\n")
    catalog.write_text('{"sentinel": "online-upgrade-v1"}\n', encoding="utf-8")
    _manage(python, install, env, "migrate", "--noinput")
    code = r'''
import hashlib
import json
import os
import django

django.setup()
from budgeting.models import BudgetCycle, Project, ProjectCycle, TemplateVersion, UploadVersion, User

project = Project.objects.create(code="UPGRADE-SENTINEL", name="在线升级哨兵项目")
admin = User.objects.create_user(
    username="upgrade-admin", password="UpgradeFixture-Admin-2026!",
    role=User.Role.ADMIN, is_staff=True, is_superuser=True,
)
project_user = User.objects.create_user(
    username="upgrade-project", password="UpgradeFixture-Project-2026!",
    role=User.Role.PROJECT, project=project,
)
template = TemplateVersion.objects.create(
    version="online-upgrade-sentinel", budget_year=2027,
    file_path="sentinel-template.xlsx", manifest_path="sentinel-template.json",
    formula_manifest_hash="a" * 64,
)
cycle = BudgetCycle.objects.create(
    name="在线升级哨兵预算", budget_year=2027, status=BudgetCycle.OPEN,
    template=template,
)
upload_path = os.environ["UPGRADE_SENTINEL_UPLOAD"]
upload_hash = hashlib.sha256(open(upload_path, "rb").read()).hexdigest()
upload = UploadVersion.objects.create(
    project=project, cycle=cycle, template=template,
    status=UploadVersion.Status.RECEIVED,
    original_name="sentinel-upload.xlsx", original_path=upload_path,
    sha256=upload_hash, note="online upgrade sentinel",
)
ProjectCycle.objects.create(project=project, cycle=cycle, current_upload=upload)
print(json.dumps({
    "project_id": project.pk, "admin_username": admin.username,
    "project_username": project_user.username, "cycle_id": cycle.pk,
    "template_id": template.pk, "upload_id": str(upload.pk).replace("-", ""),
    "upload_sha256": upload_hash,
}, ensure_ascii=False))
'''
    child_env = {**env, "UPGRADE_SENTINEL_UPLOAD": str(upload)}
    result = _run([python, "-c", code], cwd=install, env=child_env, timeout=120, label="创建升级哨兵数据")
    try:
        return json.loads(result.stdout.strip().splitlines()[-1]), {
            "upload": upload,
            "template": template,
            "catalog": catalog,
        }
    except (json.JSONDecodeError, IndexError) as exc:
        raise VerificationError("创建升级哨兵数据未返回有效记录。") from exc


def _schema(path: Path) -> int:
    with closing(sqlite3.connect(str(path))) as db:
        row = db.execute("SELECT MAX(CAST(substr(name, 1, instr(name, '_') - 1) AS INTEGER)) FROM django_migrations WHERE app='budgeting'").fetchone()
    return int(row[0] or 0)


def _table_row(db: sqlite3.Connection, table: str, column: str, value):
    columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
    if not columns:
        raise VerificationError(f"缺少哨兵表：{table}")
    quoted = ",".join('"' + item.replace('"', '""') + '"' for item in columns)
    row = db.execute(f'SELECT {quoted} FROM "{table}" WHERE "{column}"=?', (value,)).fetchone()
    if row is None:
        raise VerificationError(f"缺少哨兵数据：{table}.{column}={value}")
    return {"columns": columns, "values": list(row)}


def _sentinel_rows(path: Path, sentinel: dict):
    with closing(sqlite3.connect(str(path))) as db:
        return {
            "project": _table_row(db, "budgeting_project", "id", sentinel["project_id"]),
            "admin": _table_row(db, "budgeting_user", "username", sentinel["admin_username"]),
            "project_user": _table_row(db, "budgeting_user", "username", sentinel["project_username"]),
            "cycle": _table_row(db, "budgeting_budgetcycle", "id", sentinel["cycle_id"]),
            "template": _table_row(db, "budgeting_templateversion", "id", sentinel["template_id"]),
            "upload": _table_row(db, "budgeting_uploadversion", "id", sentinel["upload_id"]),
        }


def _compare_rows(before: dict, after: dict):
    for name, old in before.items():
        new = after.get(name)
        if new is None or not set(old["columns"]).issubset(new["columns"]):
            raise VerificationError(f"升级后哨兵表结构或字段顺序变化：{name}")
        old_values = dict(zip(old["columns"], old["values"]))
        new_values = dict(zip(new["columns"], new["values"]))
        if any(new_values[column] != value for column, value in old_values.items()):
            raise VerificationError(f"升级前后哨兵数据不一致：{name}")


def _file_hash(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"哨兵文件缺失或不安全：{path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_update_config(install: Path, port: int):
    runtime = install / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    _json_dump(runtime / "update-config.json", {"token": "fixture-token-" + "x" * 32})
    _json_dump(runtime / "server.json", {"port": port})


def _fixture_pythonpath(install: Path, provenance: str) -> str | None:
    if provenance != "fixture":
        return None
    hook_root = install.parent / "fixture updater hooks"
    hook_root.mkdir(parents=True, exist_ok=True)
    hook_lines = [
        "import os",
        "import sys",
        "import urllib.request",
        "import subprocess",
        "",
        "sys.path.insert(0, os.getcwd())",
        "_public_port = os.environ.get('UPGRADE_FIXTURE_PUBLIC_PORT')",
        "if _public_port:",
        "    import scripts.public_access as _public_access",
        "    _public_access.PORT = int(_public_port)",
        "    sys.modules['public_access'] = _public_access",
        "",
        "_fixture_api = os.environ.get('UPGRADE_FIXTURE_API')",
        "_github_api = os.environ.get('UPGRADE_FIXTURE_GITHUB_API')",
        "if _fixture_api and _github_api:",
        "    def _rewrite(value):",
        "        if isinstance(value, urllib.request.Request):",
        "            url = value.full_url",
        "            if url.startswith(_github_api + '/'):",
        "                return urllib.request.Request(",
        "                    _fixture_api + url[len(_github_api):],",
        "                    data=value.data,",
        "                    headers=dict(value.header_items()),",
        "                    origin_req_host=value.origin_req_host,",
        "                    unverifiable=value.unverifiable,",
        "                    method=value.get_method(),",
        "                )",
        "            return value",
        "        if isinstance(value, str) and value.startswith(_github_api + '/'):",
        "            return _fixture_api + value[len(_github_api):]",
        "        return value",
        "",
        "    _build_opener = urllib.request.build_opener",
        "    def build_opener(*handlers):",
        "        opener = _build_opener(*handlers)",
        "        open_method = opener.open",
        "        def open(request, *args, **kwargs):",
        "            return open_method(_rewrite(request), *args, **kwargs)",
        "        opener.open = open",
        "        return opener",
        "    urllib.request.build_opener = build_opener",
        "",
        "    _urlopen = urllib.request.urlopen",
        "    def urlopen(url, *args, **kwargs):",
        "        return _urlopen(_rewrite(url), *args, **kwargs)",
        "    urllib.request.urlopen = urlopen",
        "",
        "    _subprocess_run = subprocess.run",
        "    def run(command, *args, **kwargs):",
        "        argv = [str(item) for item in command] if isinstance(command, (list, tuple)) else []",
        "        name = os.path.basename(argv[0]).lower() if argv else ''",
        "        if name in {'gh', 'gh.exe'} and argv[1:3] == ['attestation', 'verify']:",
        "            output = '' if kwargs.get('text') or kwargs.get('universal_newlines') else b''",
        "            return subprocess.CompletedProcess(command, 0, output, output)",
        "        return _subprocess_run(command, *args, **kwargs)",
        "    subprocess.run = run",
    ]
    (hook_root / "sitecustomize.py").write_text("\n".join(hook_lines) + "\n", encoding="utf-8")
    return str(hook_root)


def _updater_action(
    python: Path,
    install: Path,
    env: dict[str, str],
    fixture: FixtureServer,
    provenance: str,
    label: str,
):
    code = r'''
import json
import os
import scripts.system_update as update

update.API = os.environ["UPGRADE_FIXTURE_API"]
if os.environ.get("UPGRADE_PROVENANCE") == "fixture":
    try:
        import scripts.release_manager as release_manager
        def fixture_attestation(archive, manifest, token=None):
            release_manager.validate_manifest(manifest)
            if manifest.get("authenticity") != "github-attestation":
                raise release_manager.ReleaseError("fixture provenance requires the GitHub attestation declaration")
        release_manager.verify_attestation = fixture_attestation
        import scripts.release_adapter as release_adapter
        release_adapter.verify_attestation = fixture_attestation
    except ImportError:
        pass

import time

checked = update.check()
started = update.start_update()
deadline = time.monotonic() + 1700
state = update.status()
while state.get("busy") or state.get("status") not in {"completed", "failed", "recovery_required"}:
    if time.monotonic() >= deadline:
        raise SystemExit("detached update timed out")
    time.sleep(0.5)
    state = update.status()
print(json.dumps({"checked": checked, "started": started, "state": state}, ensure_ascii=False))
if state.get("status") != "completed":
    raise SystemExit(2)
'''
    child_env = {
        **env,
        "UPGRADE_FIXTURE_API": fixture.api,
        "UPGRADE_PROVENANCE": provenance,
    }
    fixture_path = _fixture_pythonpath(install, provenance)
    if fixture_path:
        child_env["PYTHONPATH"] = os.pathsep.join(
            item for item in (fixture_path, child_env.get("PYTHONPATH", "")) if item
        )
        child_env["UPGRADE_FIXTURE_GITHUB_API"] = "https://api.github.com/repos/" + REPOSITORY
    result = _run([python, "-c", code], cwd=install, env=child_env, timeout=1800, label=label)
    lines = result.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} 未返回状态记录。") from exc


def _launcher(python: Path, install: Path, env: dict[str, str], port: int, action: str):
    return _run([python, install / "scripts/local_server.py", action, "--port", str(port)], cwd=install, env=env, timeout=240, label="原 launcher " + action)


def _health(port: int, timeout: int = 60):
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
                payload = json.loads(response.read(65536))
                if response.status == 200 and payload.get("database") is True and payload.get("storage") is True:
                    return payload
                last = json.dumps(payload, ensure_ascii=False)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = str(exc)
        time.sleep(0.5)
    raise VerificationError("healthz 未在限定时间内通过：" + last[-500:])


def _login(port: int, username: str, password: str):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"http://127.0.0.1:{port}/login/", timeout=10) as response:
        html = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
    match = re.search(r'name=["\']csrfmiddlewaretoken["\'][^>]*value=["\']([^"\']+)', html)
    if not match:
        match = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']csrfmiddlewaretoken["\']', html)
    if not match:
        raise VerificationError("登录页缺少 CSRF token。")
    body = urllib.parse.urlencode({
        "username": username, "password": password,
        "csrfmiddlewaretoken": match.group(1), "next": "/",
    }).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/login/", data=body,
        headers={"Referer": f"http://127.0.0.1:{port}/login/", "User-Agent": "online-upgrade-verifier"},
    )
    with opener.open(request, timeout=10) as response:
        final_url = response.geturl()
        response.read(2 * 1024 * 1024)
    if response.status != 200 or final_url.rstrip("/").endswith("/login") or final_url.rstrip("/").endswith("/login/"):
        raise VerificationError("升级后项目账号登录未成功。")
    return {"status": response.status, "final_url": final_url}


def _backup_evidence(data: Path, sentinel_files: dict):
    journal_path = data / "update-journal.json"
    if not journal_path.is_file():
        raise VerificationError("升级完成后缺少 update-journal.json 备份记录。")
    journal = _read_json(journal_path)
    backup = Path(journal.get("backup", ""))
    if not backup.is_dir():
        raise VerificationError("升级备份目录不存在。")
    required = {"database.sqlite3", "backup-manifest.json", "storage", "templates", "config"}
    missing = sorted(name for name in required if not (backup / name).exists())
    if missing:
        raise VerificationError("升级备份缺少范围：" + ", ".join(missing))
    storage_copy = backup / "storage" / sentinel_files["upload"].name
    sources = _read_json(backup / "templates" / "sources.json")
    template_copy = None
    for item in sources:
        if Path(item.get("source", "")).name == sentinel_files["template"].name:
            template_copy = backup / "templates" / item["backup"]
            break
    if template_copy is None:
        raise VerificationError("备份模板索引缺少哨兵模板。")
    if _file_hash(storage_copy) != _file_hash(sentinel_files["upload"]):
        raise VerificationError("备份中的上传原件 hash 不一致。")
    if _file_hash(template_copy) != _file_hash(sentinel_files["template"]):
        raise VerificationError("备份中的模板 hash 不一致。")
    return {
        "phase": journal.get("phase"),
        "path": str(backup),
        "database": (backup / "database.sqlite3").is_file(),
        "storage": True,
        "templates": True,
        "config": True,
        "sentinel_hashes_match": True,
    }


def verify(args) -> dict:
    bridge = args.bridge.expanduser().resolve()
    modern = args.modern.expanduser().resolve()
    artifact_evidence = _validate_inputs(bridge, modern)
    repo = args.repo.expanduser().resolve()
    if _git(repo, "rev-parse", "--is-inside-work-tree").strip() != b"true":
        raise VerificationError("--repo 不在 Git 工作树中。")
    if _git(repo, "rev-parse", BASE_COMMIT).decode().strip() != BASE_COMMIT:
        raise VerificationError("仓库缺少固定旧版基线 commit。")
    old_tag = _git(repo, "tag", "--points-at", BASE_COMMIT).decode().split()
    if "v" + OLD_VERSION not in old_tag:
        raise VerificationError("固定旧版基线没有 v" + OLD_VERSION + " 标签。")

    port = _choose_port(args.port)
    requested_workdir = args.work_dir.expanduser().resolve() if args.work_dir else None
    if requested_workdir:
        if requested_workdir.exists() and any(requested_workdir.iterdir()):
            raise VerificationError("--work-dir 必须不存在或为空目录。")
        requested_workdir.mkdir(parents=True, exist_ok=True)
        workdir = requested_workdir
    else:
        workdir = Path(tempfile.gettempdir()) / ("预算升级 隔离-" + uuid.uuid4().hex[:12])
        workdir.mkdir(parents=True, exist_ok=False)
    install = workdir / "程序 根 含中文"
    data = workdir / "业务 数据 含中文"
    data.mkdir(parents=True, exist_ok=False)
    python = Path(args.python).expanduser()
    if not python.is_absolute():
        python = Path.cwd() / python
    if not python.is_file():
        raise VerificationError(f"Python 不存在：{python}")
    running = False
    report = {
        "result": "FAIL",
        "transport": args.transport,
        "provenance": args.provenance,
        "real_github_attestation": "NOT_RUN" if args.provenance == "fixture" else "REQUESTED",
        "platform": sys.platform,
        "python": str(python),
        "port": port,
        "versions": {"old": OLD_VERSION, "bridge": BRIDGE_VERSION, "modern": MODERN_VERSION},
        "artifacts": artifact_evidence,
        "workdir": str(workdir),
        "paths_include_chinese_and_spaces": True,
        "windows_matrix": "RUN" if sys.platform == "win32" else "NOT_RUN (execute the same CLI in Windows CI)",
        "macos_matrix": "RUN" if sys.platform == "darwin" else "NOT_RUN (execute the same CLI in macOS CI)",
    }
    try:
        _safe_extract_git_archive(repo, BASE_COMMIT, install)
        env = _environment(install, data, port)
        old_system_version = _read_json(install / "system_version.json").get("version")
        if old_system_version != OLD_VERSION:
            raise VerificationError("旧版安装目录版本不是 " + OLD_VERSION + "。")
        _write_update_config(install, port)
        _manage(python, install, env, "check")
        sentinel, sentinel_files = _prepare_sentinel(python, install, data, env)
        env["UPGRADE_SENTINEL_UPLOAD"] = str(sentinel_files["upload"])
        before_rows = _sentinel_rows(Path(env["DATABASE_PATH"]), sentinel)
        before_hashes = {name: _file_hash(path) for name, path in sentinel_files.items()}
        before_schema = _schema(Path(env["DATABASE_PATH"]))
        if before_schema != 11:
            raise VerificationError(f"旧版数据库 schema 应为 11，实际为 {before_schema}。")
        report["database"] = {"before_schema": before_schema, "sentinel_rows_created": True}
        report["files"] = {"before": before_hashes}

        bridge_manifest, _ = _zip_manifest(bridge)
        modern_manifest, _ = _zip_manifest(modern)
        with FixtureServer(
            _artifact(101, bridge, bridge_manifest),
            _artifact(102, modern, modern_manifest),
        ) as fixture:
            if args.transport != "fixture":
                raise VerificationError("当前仅实现明确命名的 fixture transport。")
            running = True
            _updater_action(python, install, env, fixture, args.provenance, "旧版检查并升级 bridge")
            bridge_health = _health(port)
            bridge_version = _read_json(install / "system_version.json").get("version")
            if bridge_version != BRIDGE_VERSION:
                raise VerificationError("旧版 updater 完成后未进入 bridge 版本。")
            after_bridge_rows = _sentinel_rows(Path(env["DATABASE_PATH"]), sentinel)
            _compare_rows(before_rows, after_bridge_rows)
            if _schema(Path(env["DATABASE_PATH"])) != 11:
                raise VerificationError("bridge 阶段不应迁移数据库 schema。")

            _updater_action(python, install, env, fixture, args.provenance, "bridge 检查并升级 modern")
            modern_health = _health(port)
            pointer = _read_json(install / "active-release.json")
            active_release = Path(pointer.get("release_dir", ""))
            if pointer.get("version") != MODERN_VERSION or not active_release.is_dir():
                raise VerificationError("modern 升级完成后 active-release 指针无效。")
            after_rows = _sentinel_rows(Path(env["DATABASE_PATH"]), sentinel)
            _compare_rows(before_rows, after_rows)
            after_schema = _schema(Path(env["DATABASE_PATH"]))
            if after_schema != 16:
                raise VerificationError(f"modern 数据库 schema 应为 16，实际为 {after_schema}。")
            after_hashes = {name: _file_hash(path) for name, path in sentinel_files.items()}
            if before_hashes != after_hashes:
                raise VerificationError("升级前后哨兵原件或模板 hash 不一致。")
            backup = _backup_evidence(data, sentinel_files)

            _launcher(python, install, env, port, "stop")
            running = False
            _launcher(python, install, env, port, "start")
            running = True
            launcher_health = _health(port)
            login = _login(port, sentinel["project_username"], "UpgradeFixture-Project-2026!")
            report.update({
                "health": {"bridge": bridge_health, "modern": modern_health, "launcher_restart": launcher_health},
                "launcher": {"root": str(install / "scripts/local_server.py"), "active_release": str(active_release), "dispatch_verified": True},
                "login": login,
                "database": {"before_schema": before_schema, "after_schema": after_schema, "sentinel_rows_preserved": True},
                "files": {"before": before_hashes, "after": after_hashes, "hashes_preserved": True},
                "backup": backup,
                "result": "PASS",
            })
    finally:
        if running:
            try:
                _launcher(python, install, env, port, "stop")
            except Exception as exc:
                report["cleanup_error"] = str(exc)
        retain = bool(args.keep_workdir or report.get("result") != "PASS")
        report["workdir_retained"] = retain
        if not retain:
            shutil.rmtree(workdir, ignore_errors=True)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", type=Path, required=True, help="schema-1 bridge ZIP")
    parser.add_argument("--modern", type=Path, required=True, help="schema-2 modern ZIP")
    parser.add_argument("--repo", type=Path, default=ROOT, help="repository containing the fixed old baseline")
    parser.add_argument("--python", default=sys.executable, help="Python used for the isolated application")
    parser.add_argument("--work-dir", type=Path, help="empty directory for the isolated install and data")
    parser.add_argument("--port", type=int, help="isolated HTTP port; defaults to a free local port")
    parser.add_argument("--transport", choices=("fixture",), default="fixture", help="release transport; fixture is local HTTP only")
    parser.add_argument("--provenance", choices=("fixture", "real"), default="fixture", help="attestation mode; fixture is explicitly not GitHub attestation")
    parser.add_argument("--json-out", type=Path, help="also write the machine-readable report here")
    parser.add_argument("--keep-workdir", action="store_true", help="retain the isolated work directory for inspection")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report = None
    try:
        report = verify(args)
    except Exception as exc:
        report = {
            "result": "FAIL",
            "transport": getattr(args, "transport", "fixture"),
            "provenance": getattr(args, "provenance", "fixture"),
            "real_github_attestation": "NOT_RUN" if getattr(args, "provenance", "fixture") == "fixture" else "REQUESTED",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        if getattr(args, "keep_workdir", False):
            report["workdir_retained"] = True
    if args.json_out:
        try:
            _json_dump(args.json_out.expanduser().resolve(), report)
        except OSError as exc:
            report["json_out_error"] = str(exc)
    print(json.dumps(report, ensure_ascii=True, indent=2, default=_json_default))
    return 0 if report.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
