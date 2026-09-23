"""Install the pinned official verifier without administrator privileges."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import tempfile
import urllib.request
import zipfile

VERSION = '2.101.0'
ASSETS = {
    ('Windows', 'amd64'): ('windows_amd64', 'bc6c814367b193cd8e713611d61e36013c0ef843b8f516458fe3eda039192794'),
    ('Windows', 'arm64'): ('windows_arm64', 'e6cbb2d4afdad3e70f3d38b8d1ebaa3a0870a897cfc0e4cf569826710b96b4fd'),
    ('Darwin', 'amd64'): ('macOS_amd64', 'a6fd66c88e2f07d6e4e058173db341d07dd74d58cf8f19ae668293d2bb614ca3'),
    ('Darwin', 'arm64'): ('macOS_arm64', 'e4303e39d8f07141c4bad4b99b01079f05029c59b27076e8fbc825c985ecdd8b'),
}
MAX_DOWNLOAD = 40 * 1024 * 1024


def ensure_github_cli(runtime=None):
    machine = platform.machine().lower()
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)
    key = (platform.system(), arch)
    if key not in ASSETS:
        raise ValueError('此系统需要先安装 GitHub CLI，才能验证在线更新。')
    asset, expected = ASSETS[key]
    base = Path(runtime or os.getenv('BUDGET_RUNTIME_ROOT', Path(__file__).resolve().parents[1] / '.runtime'))
    destination = base / 'tools' / ('github-cli-' + VERSION + '-' + asset)
    executable = destination / ('gh.exe' if key[0] == 'Windows' else 'gh')
    receipt = destination / 'verified.json'
    if executable.is_file() and receipt.is_file():
        saved = json.loads(receipt.read_text(encoding='utf-8'))
        if saved.get('archive_sha256') == expected and saved.get('binary_sha256') == hashlib.sha256(executable.read_bytes()).hexdigest():
            return str(executable)
    url = f'https://github.com/cli/cli/releases/download/v{VERSION}/gh_{VERSION}_{asset}.zip'
    with urllib.request.urlopen(url, timeout=90) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('更新验证工具下载校验失败，未安装。请检查网络后重试。')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        suffix = '/bin/' + executable.name
        members = [i for i in archive.infolist() if i.filename.endswith(suffix) or i.filename == 'bin/' + executable.name]
        if len(members) != 1 or members[0].file_size > 100 * 1024 * 1024:
            raise ValueError('更新验证工具内容不完整。')
        binary = archive.read(members[0])
    destination.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination, prefix='download-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(binary)
        Path(temporary).chmod(0o700)
        os.replace(temporary, executable)
    finally:
        Path(temporary).unlink(missing_ok=True)
    receipt.write_text(json.dumps({'archive_sha256': expected, 'binary_sha256': hashlib.sha256(binary).hexdigest()}), encoding='utf-8')
    return str(executable)
