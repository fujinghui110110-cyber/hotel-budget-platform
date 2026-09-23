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
from budgeting.services.template_paths import resolve_template_path

VALUE_FIELDS = ("row_code", "row_label", "period", "month", "unit", "value_int",
                "ratio_num", "ratio_den", "source_sheet", "source_cell", "source_formula")


def canonical_rows(report_code):
    from budgeting.services.metrics import METRICS
    template = active_template()
    result = {}
    if template:
        path = resolve_template_path(template.manifest_path)
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
    """Read the upload's pinned baseline only; never retarget a prior upload."""
    cycle = BudgetCycle.objects.get(pk=upload.cycle_id)
    upload.refresh_from_db()
    if cycle.status == BudgetCycle.Status.FROZEN or upload.status in (UploadVersion.APPROVED, UploadVersion.SUPERSEDED):
        return
    if not upload.history_binding_id or upload.history_stale:
        return
    binding = upload.history_binding
    if binding.project_id != upload.project_id or binding.plan_id != cycle.plan_id:
        raise ValueError("上传历史基准与项目或年度计划不一致。")
    NormalizedValue.objects.filter(upload=upload, data_kind__in=["ACTUAL", "FORECAST"]).delete()
    NormalizedValue.objects.bulk_create([
        NormalizedValue(upload=upload, processing_run_id=upload.processing_current_run_id,
                        report_code=value.report_code, row_code=value.row_code, period=value.period,
                        data_year=value.data_year, data_kind=value.data_kind, unit=value.unit,
                        value_int=value.value_int, ratio_num=value.ratio_num, ratio_den=value.ratio_den,
                        source_sheet=value.source_sheet, source_cell=value.source_cell,
                        month=int(value.period[-2:]) if len(value.period) > 5 and value.period[-3] == 'M' else None)
        for value in binding.baseline.values.filter(data_year__lt=cycle.budget_year)
    ])


@transaction.atomic
def confirm_history(batch, selections, actor, *, plan=None, reason="", expected_revision=None, affected_plan_ids=None, include_legacy=False, legacy_import_ids=None):
    if not actor.is_admin_role:
        raise ValueError("仅管理员可以确认历史损益数据。")
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
    from budgeting.models import HistoryBaseline, PlanProject
    from budgeting.services.plan_history import confirm_history as confirm_baseline, VALUE_FIELDS as BASELINE_FIELDS
    if plan is None or expected_revision is None or not reason.strip():
        raise ValueError("请选择预算年度并填写确认理由，历史版本必须在确认页面重新核对。")
    if batch.data_year >= plan.budget_year:
        raise ValueError("历史数据年度必须早于预算年度。")
    if not PlanProject.objects.filter(plan=plan, project=batch.project).exists():
        raise ValueError("项目不在所选年度计划内。")
    previous = HistoryBaseline.objects.filter(project=batch.project).order_by('-revision').first()
    key_fields = ('report_code', 'row_code', 'data_year', 'data_kind', 'period')
    merged = {tuple(row[k] for k in key_fields): row for row in previous.values.values(*BASELINE_FIELDS)} if previous else {}
    legacy_ids = []
    if include_legacy:
        requested_ids = set(str(value) for value in (legacy_import_ids or []))
        legacy_batches = list(HistoricalImport.objects.filter(pk__in=requested_ids,project=batch.project, active=True, confirmed_at__isnull=False).prefetch_related('values'))
        if not requested_ids or {str(item.pk) for item in legacy_batches} != requested_ids:
            raise ValueError("待复核旧历史文件已变化，请刷新页面重新核对。")
        for legacy in legacy_batches:
            legacy_ids.append(str(legacy.pk))
            for value in legacy.values.all():
                row = {field: getattr(value, field) for field in BASELINE_FIELDS if field not in ('report_code','data_year','data_kind')}
                row.update(report_code=legacy.report_code,data_year=legacy.data_year,data_kind=legacy.data_kind)
                key = tuple(row[k] for k in key_fields)
                # A legacy import cannot override an already locked baseline implicitly.
                merged.setdefault(key, row)
    for value in values:
        row = {field: getattr(value, field) for field in BASELINE_FIELDS if field not in ('report_code','data_year','data_kind')}
        row.update(report_code=batch.report_code,data_year=batch.data_year,data_kind=batch.data_kind)
        merged[tuple(row[k] for k in key_fields)] = row
    baseline = confirm_baseline(plan=plan, project=batch.project, values=list(merged.values()), actor=actor,
        reason=reason, expected_revision=expected_revision, affected_plan_ids=affected_plan_ids,
        source_identity={'import_id':str(batch.pk),'sha256':batch.sha256,'original_name':batch.original_name,
                         'reconfirmed_legacy_import_ids':legacy_ids})
    HistoricalImport.objects.filter(project=batch.project, data_year=batch.data_year, data_kind=batch.data_kind,
        report_code=batch.report_code, active=True).update(active=False)
    HistoricalValue.objects.bulk_create(values)
    batch.proposal['confirmed_mapping'] = selections
    batch.proposal['history_baseline_id'] = baseline.pk
    batch.proposal['confirmation_reason'] = reason
    batch.active = True
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=['active','confirmed_at','proposal'])
    audit(actor, 'HISTORY_CONFIRMED', 'HistoricalImport', batch.pk,
          {'rows':len(used),'values':len(values),'year':batch.data_year,'kind':batch.data_kind,
           'baseline_id':baseline.pk,'reason':reason}, project=batch.project)
    return batch
