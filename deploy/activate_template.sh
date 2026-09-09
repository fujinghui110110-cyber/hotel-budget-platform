#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$ROOT_DIR"

if [ "$#" -ne 1 ]; then
    echo "用法：$0 <预算年度>" >&2
    echo "示例：$0 2027" >&2
    exit 2
fi

YEAR=$1
case "$YEAR" in
    ''|*[!0-9]*) echo "预算年度必须是数字：$YEAR" >&2; exit 2 ;;
esac
if [ "$YEAR" -lt 2000 ] || [ "$YEAR" -gt 2100 ]; then
    echo "预算年度必须在 2000—2100 之间：$YEAR" >&2
    exit 2
fi

TEMPLATE="artifacts/v3/$YEAR/平台标准预算模板_V3_${YEAR}.xlsx"
MANIFEST="artifacts/v3/$YEAR/template_manifest_V3_${YEAR}.json"
if [ ! -f "$TEMPLATE" ] || [ ! -f "$MANIFEST" ]; then
    echo "缺少最终 V3 清洁模板或 manifest：$TEMPLATE / $MANIFEST" >&2
    exit 1
fi

export BUDGET_YEAR="$YEAR" BUDGET_TEMPLATE="$TEMPLATE" BUDGET_MANIFEST="$MANIFEST"
PYTHON_BIN=${PYTHON_BIN:-python}
"$PYTHON_BIN" manage.py shell <<'PY'
import hashlib
import json
import os
from pathlib import Path

from django.conf import settings
from django.db import transaction
from budgeting.excel.ooxml import formula_manifest

from budgeting.models import BudgetCycle, TemplateVersion

year = int(os.environ["BUDGET_YEAR"])
template = Path(os.environ["BUDGET_TEMPLATE"]).resolve()
manifest_path = Path(os.environ["BUDGET_MANIFEST"]).resolve()
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if int(manifest.get("budget_year", -1)) != year:
    raise SystemExit("manifest 的 budget_year 与命令参数不一致")
version = str(manifest.get("template_version", ""))
if version != f"V3-{year}":
    raise SystemExit(f"只允许登记最终 V3 manifest，实际版本为：{version}")
formula_hash = str(manifest.get("formula_manifest_hash", "")).strip().lower()
if len(formula_hash) != 64:
    raise SystemExit("manifest 缺少有效 formula_manifest_hash")
if formula_manifest(template)[1] != formula_hash:
    raise SystemExit("模板公式指纹与 manifest 不一致，未登记模板")

base = Path(settings.BASE_DIR)
file_path = template.relative_to(base).as_posix()
manifest_rel = manifest_path.relative_to(base).as_posix()
with transaction.atomic():
    TemplateVersion.objects.filter(budget_year=year).exclude(version=version).update(is_active=False)
    registered, created = TemplateVersion.objects.update_or_create(
        version=version,
        defaults={
            "budget_year": year,
            "file_path": file_path,
            "manifest_path": manifest_rel,
            "formula_manifest_hash": formula_hash,
            "rule_version": str(manifest.get("rule_version", "R3")),
            "is_active": True,
        },
    )
    
    # A cycle with no template may safely adopt the explicitly activated year
    # template. Existing cycle/template bindings are preserved.
    bound_cycles = []
    for cycle in BudgetCycle.objects.filter(budget_year=year, template__isnull=True):
        cycle.template = registered
        cycle.save(update_fields=["template"])
        bound_cycles.append(cycle.pk)

digest = hashlib.sha256(template.read_bytes()).hexdigest()
print(json.dumps({
    "status": "created" if created else "updated",
    "template_version": registered.version,
    "budget_year": year,
    "file": file_path,
    "manifest": manifest_rel,
    "sha256": digest,
    "bound_untemplated_cycles": bound_cycles,
}, ensure_ascii=False))
PY
