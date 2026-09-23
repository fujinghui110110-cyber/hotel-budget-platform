"""One isolated LibreOffice process tree per workbook conversion."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class RecalcInfrastructureError(RuntimeError):
    pass


def discover_soffice(configured=None):
    explicit = configured or os.getenv("SOFFICE_BIN")
    if explicit:
        selected = Path(explicit).expanduser()
        if not selected.is_file():
            raise RecalcInfrastructureError(f"LibreOffice 配置路径不存在：{selected}")
        return selected
    candidates = [shutil.which("soffice"), shutil.which("libreoffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice", "/opt/homebrew/bin/soffice",
        str(Path(os.getenv("ProgramFiles", "C:/Program Files")) / "LibreOffice/program/soffice.exe"),
        str(Path(os.getenv("ProgramFiles(x86)", "C:/Program Files (x86)")) / "LibreOffice/program/soffice.exe")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RecalcInfrastructureError("未找到 LibreOffice，请安装或设置 SOFFICE_BIN。")


def recalc_with_libreoffice(workbook_path, soffice_bin=None, timeout=180):
    from scripts.runtime_support import detached_popen_kwargs, stop_process_tree
    source = Path(workbook_path)
    soffice = discover_soffice(soffice_bin)
    try:
        with tempfile.TemporaryDirectory(prefix="budget-lo-") as tmp:
            root = Path(tmp)
            in_dir, out_dir, profile = root / "input", root / "output", root / "profile"
            in_dir.mkdir(); out_dir.mkdir()
            work = in_dir / source.name
            shutil.copy2(source, work)
            process = subprocess.Popen([str(soffice), "--headless",
                f"-env:UserInstallation={profile.resolve().as_uri()}", "--convert-to", "xlsx",
                "--outdir", str(out_dir), str(work)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, **detached_popen_kwargs())
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                stop_process_tree(process.pid, timeout=5)
                process.communicate(timeout=5)
                raise RecalcInfrastructureError("LibreOffice 重算超时，已停止本任务的进程树。") from exc
            if process.returncode:
                raise RecalcInfrastructureError((stderr or stdout or "LibreOffice 重算失败")[-2000:])
            produced = out_dir / (work.stem + ".xlsx")
            if not produced.is_file() or not produced.stat().st_size:
                raise RecalcInfrastructureError("LibreOffice 未生成有效的重算文件。")
            target = source.with_name(source.stem + ".recalculated.xlsx")
            shutil.copy2(produced, target)
            return target
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecalcInfrastructureError(str(exc)) from exc
