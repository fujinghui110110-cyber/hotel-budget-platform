import hashlib
import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError

MAX_ZIP = 50 * 1024 * 1024
MAX_EXPANDED = 500 * 1024 * 1024
MAX_RATIO = 100


def rel(path: Path) -> str:
    return str(path.relative_to(settings.BASE_DIR))


def abs_path(relative: str) -> Path:
    return settings.BASE_DIR / relative


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_dir() -> Path:
    path = settings.BUDGET_STORAGE_ROOT / "uploads" / str(uuid.uuid4())
    path.mkdir(parents=True, exist_ok=False)
    return path


def check_xlsx_package(path: Path):
    if path.suffix.lower() != ".xlsx":
        raise ValidationError("仅允许上传 .xlsx 文件。")
    if path.stat().st_size > MAX_ZIP:
        raise ValidationError("压缩文件超过 50 MiB。")
    expanded = 0
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = info.filename
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts:
                raise ValidationError("OOXML 包含路径穿越。")
            expanded += info.file_size
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                raise ValidationError("压缩比超过安全限制。")
            lower = name.lower()
            if lower.endswith(".bin") or "/vba" in lower or "vbaproject" in lower:
                raise ValidationError("不允许包含宏。")
            if "externallinks/" in lower or "connections" in lower or "oleobjects/" in lower:
                raise ValidationError("不允许包含外链、连接或 OLE。")
    if expanded > MAX_EXPANDED:
        raise ValidationError("解压后内容超过 500 MiB。")


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def atomic_replace_dir(staging: Path, final: Path):
    if final.exists():
        raise FileExistsError(final)
    os.replace(staging, final)


def copy_file(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
