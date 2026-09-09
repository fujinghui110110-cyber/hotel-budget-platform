import shutil
import subprocess
import tempfile
from pathlib import Path


class RecalcInfrastructureError(RuntimeError):
    pass


def recalc_with_libreoffice(workbook_path, soffice_bin="/opt/homebrew/bin/soffice", timeout=180):
    source = Path(workbook_path)
    soffice = Path(soffice_bin)
    if not soffice.exists():
        raise RecalcInfrastructureError(f"LibreOffice not found: {soffice}")
    try:
        with tempfile.TemporaryDirectory(prefix="budget-lo-") as tmp:
            tmp_path = Path(tmp)
            in_dir = tmp_path / "in"
            out_dir = tmp_path / "out"
            profile = tmp_path / "profile"
            in_dir.mkdir()
            out_dir.mkdir()
            work = in_dir / source.name
            shutil.copy2(source, work)
            proc = subprocess.run(
                [
                    str(soffice),
                    "--headless",
                    f"-env:UserInstallation=file://{profile}",
                    "--convert-to",
                    "xlsx",
                    "--outdir",
                    str(out_dir),
                    str(work),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode:
                raise RecalcInfrastructureError((proc.stderr or proc.stdout or "LibreOffice failed").strip())
            produced = out_dir / work.name
            if not produced.exists() or produced.stat().st_size == 0:
                raise RecalcInfrastructureError("LibreOffice did not create a recalculated workbook")
            final = source.with_name(source.stem + ".recalculated.xlsx")
            shutil.copy2(produced, final)
            return final
    except subprocess.TimeoutExpired as exc:
        raise RecalcInfrastructureError("LibreOffice recalculation timed out") from exc
    except OSError as exc:
        raise RecalcInfrastructureError(str(exc)) from exc
