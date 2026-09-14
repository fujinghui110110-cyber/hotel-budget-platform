"""Separate historical sources and the normalized read cache used by budget screens."""
import hashlib
import json
import shutil
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from budgeting.models import BudgetCycle, HistoricalImport, HistoricalValue, NormalizedValue, Project, UploadVersion
from budgeting.services.workflow import active_template, audit

VALUE_FIELDS = ("row_code", "row_label", "period", "month", "unit", "value_int",
                "ratio_num", "ratio_den", "source_sheet", "source_cell", "source_formula")


def canonical_rows(report_code):
    from budgeting.services.metrics import METRICS
    template = active_template()
    result = {}
    if template:
        path = Path(template.manifest_path)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
        manifest = json.loads(path.read_text())
        for item in manifest.get("reports", {}).get(report_code, {}).get("mapping", []):
            result[item["row_code"]] = {key: item.get(key, "") for key in ("row_code", "row_label", "unit", "aggregation")}
    for metric in METRICS.values():
        code = metric.get("rows", {}).get(report_code)
        if code and code not in result:
            result[code] = {"row_code": code, "row_label": metric["label"], "unit": metric["unit"], "aggregation": metric.get("aggregation", "SUM")}
    return list(result.values())


def stage_history(project, file, actor, data_year, data_kind, report_code, money_unit):
    from budgeting.services.history_parser import propose_history
    if not actor.is_admin_role:
        raise ValueError("仅管理员可以上传历史损益汇总。")
    if Path(file.name).suffix.lower() not in {".xlsx", ".xlsm"} or file.size > 50 * 1024 * 1024:
        raise ValueError("请上传不超过 50 MiB 的 .xlsx 或 .xlsm 文件。")
    folder = Path(settings.BUDGET_STORAGE_ROOT) / "historical" / str(uuid.uuid4())
    folder.mkdir(parents=True)
    target = folder / ("original" + Path(file.name).suffix.lower())
    try:
        with target.open("wb") as stream:
            for chunk in file.chunks():
                stream.write(chunk)
        canonical = canonical_rows(report_code)
        proposal = propose_history(target, canonical, data_year, data_kind, report_code, money_unit)
        if not proposal.get("rows"):
            raise ValueError("未识别到可供确认的科目，请检查科目名称、年度和金额列。")
        proposal["canonical"] = canonical
        batch = HistoricalImport.objects.create(
            project=project, data_year=data_year, data_kind=data_kind, report_code=report_code,
            money_unit=money_unit, original_name=file.name,
            original_path=str(target.relative_to(settings.BUDGET_STORAGE_ROOT)),
            sha256=hashlib.sha256(target.read_bytes()).hexdigest(), proposal=proposal, created_by=actor,
        )
        audit(actor, "HISTORY_PREVIEW", "HistoricalImport", batch.pk, {"file": file.name}, project=project)
        return batch
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


@transaction.atomic
def sync_history(upload):
    """Refresh historical read caches; frozen budget snapshots are immutable."""
    cycle = BudgetCycle.objects.select_for_update().get(pk=upload.cycle_id)
    if cycle.status == BudgetCycle.Status.FROZEN:
        return
    Project.objects.select_for_update().get(pk=upload.project_id)
    NormalizedValue.objects.filter(upload=upload, data_kind__in=["ACTUAL", "FORECAST"]).delete()
    batches = HistoricalImport.objects.filter(project_id=upload.project_id, active=True).prefetch_related("values")
    for batch in batches:
        if batch.data_year >= cycle.budget_year:
            continue
        # A confirmed file replaces the full selected report/year/kind, including blank subjects.
        NormalizedValue.objects.bulk_create([
            NormalizedValue(upload=upload, history_import=batch, report_code=batch.report_code,
                            data_year=batch.data_year, data_kind=batch.data_kind,
                            **{field: getattr(value, field) for field in VALUE_FIELDS})
            for value in batch.values.all()
        ])


@transaction.atomic
def confirm_history(batch, selections, actor):
    if not actor.is_admin_role:
        raise ValueError("仅管理员可以确认历史损益数据。")
    # Use the same cycle -> project lock ordering as sync_history.
    cycles = list(BudgetCycle.objects.select_for_update().exclude(status=BudgetCycle.Status.FROZEN))
    Project.objects.select_for_update().get(pk=batch.project_id)
    batch = HistoricalImport.objects.select_for_update().get(pk=batch.pk)
    if batch.confirmed_at:
        raise ValueError("该文件已经确认，请重新上传需要替换的文件。")
    canonical = {item["row_code"]: item for item in batch.proposal["canonical"]}
    values, used, periods = [], set(), set()
    for row in batch.proposal["rows"]:
        code = selections.get(row["source_key"], "")
        if not code:
            continue
        if code not in canonical:
            raise ValueError("所选系统科目无效，请重新选择。")
        if code in used:
            raise ValueError("多条原科目映射到同一系统科目，请保留其中一条；系统不会自动重复累加。")
        used.add(code)
        meta = canonical[code]
        for item in row.get("values", []):
            if item["unit"] != meta["unit"]:
                raise ValueError(f"“{row['source_label']}”与所选科目的单位不同，请核对后重新上传。")
            month = item.get("month")
            if (code, month) in periods:
                raise ValueError(f"“{row['source_label']}”同一期间识别到多列金额，请在原表明确保留所选年度口径的一列后重新上传。")
            periods.add((code, month))
            prefix = "A" if batch.data_kind == "ACTUAL" else "F"
            values.append(HistoricalValue(
                import_batch=batch, row_code=code, row_label=meta["row_label"],
                period=f"{prefix}{batch.data_year}" + (f"M{month:02d}" if month else ""),
                **{field: item.get(field, "" if field.startswith("source") else None)
                   for field in ("month", "unit", "value_int", "ratio_num", "ratio_den", "source_sheet", "source_cell", "source_formula")},
            ))
    if not values:
        raise ValueError("请至少选择一个有可读取数值的科目。")
    HistoricalImport.objects.filter(project=batch.project, data_year=batch.data_year,
                                    data_kind=batch.data_kind, report_code=batch.report_code, active=True).update(active=False)
    HistoricalValue.objects.bulk_create(values)
    batch.proposal["confirmed_mapping"] = selections
    batch.active = True
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=["active", "confirmed_at", "proposal"])
    for upload in UploadVersion.objects.filter(project=batch.project, cycle_id__in=[c.pk for c in cycles]):
        sync_history(upload)
    audit(actor, "HISTORY_CONFIRMED", "HistoricalImport", batch.pk,
          {"rows": len(used), "values": len(values), "year": batch.data_year, "kind": batch.data_kind},
          project=batch.project)
    return batch
