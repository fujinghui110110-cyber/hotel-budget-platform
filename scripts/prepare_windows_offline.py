"""Download pinned official Windows installers and dependency wheels for releases."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def verified(path, expected):
    if not path.is_file():
        return False
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest() == expected


def main():
    manifest = json.loads((ROOT / 'scripts/windows_dependencies.json').read_text())
    destination = ROOT / 'offline/windows'
    destination.mkdir(parents=True, exist_ok=True)
    for key in ('python', 'libreoffice', 'cloudflared'):
        item = manifest[key]
        target = destination / item['filename']
        if not verified(target, item['sha256']):
            partial = target.with_suffix(target.suffix + '.partial')
            print('Downloading', item['filename'], flush=True)
            urllib.request.urlretrieve(item['url'], partial)
            if not verified(partial, item['sha256']):
                raise RuntimeError('Official installer checksum mismatch: ' + item['filename'])
            partial.replace(target)
        print('Verified', item['filename'], flush=True)
    wheels = destination / 'wheels'
    wheels.mkdir(exist_ok=True)
    requirements = '\n'.join(line for line in (ROOT / 'requirements.txt').read_text().splitlines()
                             if not line.startswith('gunicorn'))
    with tempfile.TemporaryDirectory() as temporary:
        req = Path(temporary) / 'requirements.txt'
        # pip evaluates platform_system markers on the packaging host, even
        # with --platform. Django requires tzdata specifically on Windows.
        req.write_text(requirements + '\ntzdata\n')
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--platform', 'win_amd64',
                        '--python-version', '313', '--implementation', 'cp', '--abi', 'cp313',
                        '--only-binary=:all:', '-r', str(req), '-d', str(wheels)], check=True)
    hashes = {}
    for wheel in sorted(wheels.glob('*.whl')):
        with wheel.open('rb') as source:
            hashes[wheel.name] = hashlib.file_digest(source, 'sha256').hexdigest()
    (wheels / 'sha256.json').write_text(json.dumps(hashes, indent=2) + '\n')
    print('Offline Windows dependencies ready:', destination)


if __name__ == '__main__':
    main()
