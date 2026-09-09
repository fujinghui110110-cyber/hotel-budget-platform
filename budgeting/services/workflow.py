import json
import shutil
import uuid
import zipfile
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone
from openpyxl import Workbook

from budgeting.excel.extract import _load_manifest, extract_report_values, extract_sub_table_values, sub_table_specs
from budgeting.excel.supplementary import extract_supplementary_values
from budgeting.excel.channel_checks import validate_channel_values
from budgeting.excel.business_checks import validate_management_values
from budgeting.excel.history import extract_management_values
from budgeting.excel.ooxml import (
    cached_errors,
    formula_manifest,
    sha256_file,
    validate_upload_contract,
    validate_xlsx_zip,
)
from budgeting.excel.recalc import RecalcInfrastructureError, recalc_with_libreoffice
from budgeting.models import (
    AdjustmentBatch,
    AdjustmentLine,
    AuditEvent,
    BudgetCycle,
    FreezeSnapshot,
    NormalizedValue,
    ProcessingJob,
    Project,
    ProjectCycle,
    REPORTS,
    SnapshotArtifact,
    TemplateVersion,
    UploadVersion,
    ValidationIssue,
    ValidationRun,
)
from budgeting.services.allocations import largest_remainder
from budgeting.services.drivers import ROOM_REV, simulate_driver
from budgeting.services.pnl_graph import MONTHS, build_report_graph, load_manifest, recompute


class InfrastructureProcessingError(RuntimeError):
    pass


RATIO_SCALE = 10_000


def _lock_mutable_cycle(cycle):
    cycle = BudgetCycle.objects.select_for_update().get(pk=cycle.pk)
    if cycle.status == BudgetCycle.Status.FROZEN:
        raise ValueError("冻结周期不允许修改")
    return cycle


def _verify_original_hash(upload, source_path=None):
    source_path = source_path or (settings.BUDGET_STORAGE_ROOT / upload.original_path)
    if not source_path.exists():
        raise ValueError("上传原始文件不存在，不能生成冻结快照")
    expected = (upload.sha256 or "").strip().lower()
    actual = sha256_file(source_path).lower()
    if not expected or actual != expected:
        raise ValueError(f"上传原始文件哈希不匹配：{upload.id}")
    return source_path


def _issue_parts(issue):
    severity, code, message = issue[:3]
    location = issue[3] if len(issue) > 3 else ""
    actual_value = issue[4] if len(issue) > 4 else ""
    expected_value = issue[5] if len(issue) > 5 else ""
    return severity, code, message, location, actual_value, expected_value


def active_cycle():
    return BudgetCycle.objects.exclude(status=BudgetCycle.Status.FROZEN).order_by("-budget_year", "-revision_no").first()


def active_template(cycle=None):
    if cycle and cycle.template_id:
        return cycle.template
    return TemplateVersion.objects.filter(is_active=True).order_by("-created_at").first()


def audit(actor, action, object_type, object_id, payload=None, project=None, cycle=None, upload=None):
    AuditEvent.objects.create(
        actor=actor,
        action=action,
        project=project,
        cycle=cycle,
        upload=upload,
        payload={"object_type": object_type, "object_id": str(object_id), **(payload or {})},
    )


def save_upload(project, cycle, uploaded_file):
    if cycle is None:
        raise ValueError("当前没有开放预算周期")
    cycle = BudgetCycle.objects.get(pk=cycle.pk)
    project = Project.objects.get(pk=project.pk)
    if cycle.status not in (BudgetCycle.Status.OPEN, BudgetCycle.Status.ADJUSTING):
        raise ValueError("当前周期不允许上传")
    project_cycle = ProjectCycle.objects.filter(project=project, cycle=cycle).first()
    if project_cycle and not project_cycle.is_open:
        raise ValueError("项目周期已关闭，不能上传")
    if not uploaded_file.name.lower().endswith(".xlsx"):
        raise ValueError("仅允许上传 .xlsx 文件")
    if uploaded_file.size > 50 * 1024 * 1024:
        raise ValueError("压缩文件超过 50 MiB")
    template = active_template(cycle)
    if template is None:
        raise ValueError("当前周期没有可用模板")

    upload_id = uuid.uuid4()
    rel_dir = Path("uploads") / project.code / str(upload_id)
    abs_dir = settings.BUDGET_STORAGE_ROOT / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    dest = abs_dir / "original.xlsx"
    with dest.open("wb") as fh:
        for chunk in uploaded_file.chunks():
            fh.write(chunk)
    digest = sha256_file(dest)
    existing = UploadVersion.objects.filter(project=project, cycle=cycle, sha256=digest).order_by("-created_at").first()
    if existing:
        shutil.rmtree(abs_dir, ignore_errors=True)
        return existing
    upload = UploadVersion.objects.create(
        id=upload_id,
        project=project,
        cycle=cycle,
        template=template,
        original_name=uploaded_file.name,
        original_path=str(rel_dir / "original.xlsx"),
        sha256=digest,
    )
    ProcessingJob.objects.get_or_create(upload=upload, idempotency_key=f"upload:{upload.id}")
    audit(None, "UPLOAD_RECEIVED", "UploadVersion", upload.id, {"file": uploaded_file.name}, project=project, cycle=cycle, upload=upload)
    return upload


def process_upload(upload):
    source_path = settings.BUDGET_STORAGE_ROOT / upload.original_path
    run = ValidationRun.objects.create(upload=upload, rule_version=upload.template.rule_version if upload.template else "rules-v1")
    if not source_path.exists():
        ValidationIssue.objects.create(
            run=run,
            severity=ValidationIssue.Severity.P0,
            code="STORED_FILE_MISSING",
            message="上传原始文件不存在，不能处理。",
            location=upload.original_path,
        )
        run.passed = False
        run.save(update_fields=["passed"])
        upload.status = UploadVersion.Status.REJECTED
        upload.save(update_fields=["status"])
        return False
    if sha256_file(source_path) != upload.sha256:
        ValidationIssue.objects.create(
            run=run, severity="P0", code="STORED_FILE_HASH_MISMATCH",
            message="上传原件哈希与接收记录不一致，不能处理。", location=upload.original_path,
        )
        upload.status = UploadVersion.Status.REJECTED
        upload.save(update_fields=["status"])
        return False
    for issue in [*validate_xlsx_zip(source_path), *validate_upload_contract(upload, source_path)]:
        severity, code, message, location, actual_value, expected_value = _issue_parts(issue)
        ValidationIssue.objects.create(
            run=run,
            severity=severity,
            code=code,
            message=message,
            location=location,
            actual_value=actual_value,
            expected_value=expected_value,
        )
    _record_formula_mapping_issues(run, upload, source_path)
    if run.issues.filter(severity=ValidationIssue.Severity.P0).exists():
        run.passed = False
        run.save(update_fields=["passed"])
        upload.status = UploadVersion.Status.REJECTED
        upload.save(update_fields=["status"])
        return False
    try:
        recalculated = recalc_with_libreoffice(source_path, settings.SOFFICE_BIN)
    except RecalcInfrastructureError as exc:
        raise InfrastructureProcessingError(str(exc)) from exc
    for error in cached_errors(recalculated):
        ValidationIssue.objects.create(
            run=run, severity="P0", code="RECALCULATED_EXCEL_ERROR",
            message=f"服务端重算产生 Excel 错误：{error['value']}。",
            location=f"{error['sheet']}!{error['cell']}",
        )
    count = extract_report_values(upload, recalculated, validation_run=run)
    if not _load_manifest(upload).get("management_v2") or _load_manifest(upload).get("management_v3"):
        extract_sub_table_values(upload, recalculated)
    extract_management_values(upload, recalculated, validation_run=run)
    if _load_manifest(upload).get("management_v3"):
        extract_supplementary_values(upload, recalculated)
        validate_channel_values(recalculated, run)
    if _load_manifest(upload).get("management_v2"):
        validate_management_values(upload, run)
    upload.recalculated_path = str(recalculated.relative_to(settings.BUDGET_STORAGE_ROOT))
    if count == 0:
        ValidationIssue.objects.create(
            run=run,
            severity=ValidationIssue.Severity.P0,
            code="NO_REPORT_VALUES",
            message="四张固定报表未抽取到标准化值",
        )
    run.passed = not run.issues.filter(severity=ValidationIssue.Severity.P0).exists()
    run.save(update_fields=["passed"])
    upload.status = UploadVersion.Status.VALIDATED if run.passed else UploadVersion.Status.REJECTED
    upload.save(update_fields=["status", "recalculated_path"])
    return run.passed


@transaction.atomic
def submit_upload(upload, actor=None):
    upload = UploadVersion.objects.select_for_update().get(pk=upload.pk)
    if upload.cycle.status == BudgetCycle.Status.FROZEN:
        raise ValueError("冻结周期不能提交版本")
    has_p0 = ValidationIssue.objects.filter(run__upload=upload, severity=ValidationIssue.Severity.P0).exists()
    if upload.status != UploadVersion.Status.VALIDATED or has_p0:
        raise ValueError("只有无 P0 的已校验版本可以提交")
    if any(not issue.acknowledgement_note.strip() for issue in ValidationIssue.objects.filter(run__upload=upload, severity="P1")):
        raise ValueError("请先填写所有 P1 波动事项的说明")
    upload.status = UploadVersion.Status.SUBMITTED
    upload.submitted_at = timezone.now()
    upload.save(update_fields=["status", "submitted_at"])
    update_adjustment_lines_for_upload(upload)
    audit(actor, "UPLOAD_SUBMITTED", "UploadVersion", upload.id, project=upload.project, cycle=upload.cycle, upload=upload)
    return upload


@transaction.atomic
def approve_upload(upload, actor=None):
    upload = UploadVersion.objects.select_for_update().get(pk=upload.pk)
    if upload.cycle.status == BudgetCycle.Status.FROZEN:
        raise ValueError("冻结周期不能批准或替换版本")
    if upload.status != UploadVersion.Status.SUBMITTED:
        raise ValueError("只有已提交版本可以批准")
    if ValidationIssue.objects.filter(run__upload=upload, severity=ValidationIssue.Severity.P0).exists():
        raise ValueError("存在 P0 问题，不能批准")
    p1_open = any(not issue.acknowledged or not issue.acknowledgement_note.strip()
                  for issue in ValidationIssue.objects.filter(run__upload=upload, severity=ValidationIssue.Severity.P1))
    if p1_open:
        raise ValueError("存在未确认 P1 问题，不能批准")
    for line in AdjustmentLine.objects.select_for_update().filter(
        project=upload.project, cycle=upload.cycle, status=AdjustmentLine.Status.OPEN,
        batch__status=AdjustmentBatch.Status.ISSUED,
    ):
        value = NormalizedValue.objects.filter(
            upload=upload, report_code=line.report_code, row_code=line.row_code,
            period=line.period, unit=NormalizedValue.Unit.MONEY,
        ).first()
        if value is None or value.value_int != line.target_cents:
            raise ValueError(f"调整 {line.batch_id} 尚未精确落实到分，不能替换正式版本")
    pc, _ = ProjectCycle.objects.select_for_update().get_or_create(project=upload.project, cycle=upload.cycle, defaults={"current_upload": upload})
    old = pc.current_upload if pc.current_upload_id != upload.id else None
    upload.status = UploadVersion.Status.APPROVED
    upload.approved_at = timezone.now()
    upload.save(update_fields=["status", "approved_at"])
    pc.current_upload = upload
    pc.save(update_fields=["current_upload"])
    if old:
        old.status = UploadVersion.Status.SUPERSEDED
        old.save(update_fields=["status"])
    update_adjustment_lines_for_upload(upload)
    audit(actor, "UPLOAD_APPROVED", "UploadVersion", upload.id, project=upload.project, cycle=upload.cycle, upload=upload)
    return upload


@transaction.atomic
def reject_upload(upload, actor=None, reason=""):
    upload = UploadVersion.objects.select_for_update().get(pk=upload.pk)
    _lock_mutable_cycle(upload.cycle)
    if upload.status != UploadVersion.Status.SUBMITTED:
        raise ValueError("只有已提交版本可以打回")
    upload.status = UploadVersion.Status.REJECTED
    upload.note = (reason or "").strip()
    upload.save(update_fields=["status", "note"])
    audit(actor, "UPLOAD_REJECTED", "UploadVersion", upload.id, {"reason": upload.note}, project=upload.project, cycle=upload.cycle, upload=upload)
    return upload


def _claim_inline_upload_job(upload):
    job = (
        ProcessingJob.objects
        .filter(upload_id=upload.pk)
        .order_by("created_at")
        .first()
    )
    if job is None:
        job, _ = ProcessingJob.objects.get_or_create(
            idempotency_key=f"upload:{upload.pk}",
            defaults={"upload": upload},
        )
        job.refresh_from_db()
    if job.status != ProcessingJob.Status.QUEUED:
        return job, False

    now = timezone.now()
    claimed = ProcessingJob.objects.filter(
        pk=job.pk,
        status=ProcessingJob.Status.QUEUED,
    ).update(
        status=ProcessingJob.Status.RUNNING,
        attempts=F("attempts") + 1,
        lease_until=None,
        heartbeat_at=now,
    )
    if not claimed:
        job.refresh_from_db()
        upload.refresh_from_db(fields=["status", "note", "recalculated_path"])
        return job, False
    job.refresh_from_db()
    UploadVersion.objects.filter(pk=upload.pk).update(status=UploadVersion.Status.PROCESSING)
    upload.status = UploadVersion.Status.PROCESSING
    return job, True


def process_upload_now(upload):
    job, claimed = _claim_inline_upload_job(upload)
    if not claimed:
        upload.refresh_from_db(fields=["status", "note", "recalculated_path"])
        return job
    try:
        process_upload(upload)
    except InfrastructureProcessingError as exc:
        upload.status = UploadVersion.Status.REJECTED
        upload.note = str(exc)
        upload.save(update_fields=["status", "note"])
        job.status = ProcessingJob.Status.FAILED
        job.error = str(exc)
    except Exception as exc:
        upload.status = UploadVersion.Status.REJECTED
        upload.note = str(exc)
        upload.save(update_fields=["status", "note"])
        job.status = ProcessingJob.Status.FAILED
        job.error = str(exc)
    else:
        job.status = ProcessingJob.Status.DONE
        job.error = ""
    job.lease_until = None
    job.heartbeat_at = timezone.now()
    job.save(update_fields=["status", "error", "lease_until", "heartbeat_at"])
    return job


def _round_ratio(num, den):
    if not den:
        return 0
    return int(
        (Decimal(num) * Decimal(RATIO_SCALE) / Decimal(den)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _detail_for_rows(rows):
    """Aggregate one or more NormalizedValue rows into a single detail dict."""
    units = {row.unit for row in rows}
    unit = rows[0].unit
    if units == {NormalizedValue.Unit.RATIO}:
        ratio_num = sum(int(row.ratio_num or 0) for row in rows)
        ratio_den = sum(int(row.ratio_den or 0) for row in rows)
        return {
            "unit": unit,
            "value_int": _round_ratio(ratio_num, ratio_den),
            "ratio_num": ratio_num,
            "ratio_den": ratio_den,
        }
    if units == {NormalizedValue.Unit.MONEY} and all(
        row.ratio_num is not None and row.ratio_den is not None for row in rows
    ):
        ratio_num = sum(int(row.ratio_num or 0) for row in rows)
        ratio_den = sum(int(row.ratio_den or 0) for row in rows)
        return {
            "unit": unit,
            "value_int": (
                int(
                    (Decimal(ratio_num) / Decimal(ratio_den)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                if ratio_den
                else 0
            ),
            "ratio_num": ratio_num,
            "ratio_den": ratio_den,
        }
    return {
        "unit": unit,
        "value_int": sum(int(row.value_int or 0) for row in rows),
        "ratio_num": None,
        "ratio_den": None,
    }


def _company_value_details(cycle, report_code):
    grouped = defaultdict(list)
    if cycle is None:
        return {}
    current_ids = ProjectCycle.objects.filter(
        cycle=cycle,
        current_upload__cycle=cycle,
        current_upload__project=F("project"),
        current_upload__status=UploadVersion.Status.APPROVED,
        project__is_active=True,
    ).values_list("current_upload_id", flat=True)
    for value in NormalizedValue.objects.filter(upload_id__in=current_ids, report_code=report_code):
        grouped[(value.row_code, value.period)].append(value)
    return {key: _detail_for_rows(rows) for key, rows in grouped.items()}


def project_value_details(project, cycle, report_code):
    """Return ``{(row_code, period): detail}`` for one project's approved upload."""
    if cycle is None:
        return {}
    pc = ProjectCycle.objects.filter(
        project=project,
        cycle=cycle,
        current_upload__cycle=cycle,
        current_upload__project=F("project"),
        current_upload__status=UploadVersion.Status.APPROVED,
        project__is_active=True,
    ).select_related("current_upload").first()
    if not pc:
        return {}
    grouped = defaultdict(list)
    for value in NormalizedValue.objects.filter(upload=pc.current_upload, report_code=report_code):
        grouped[(value.row_code, value.period)].append(value)
    return {key: _detail_for_rows(rows) for key, rows in grouped.items()}


def company_values(cycle, report_code):
    return {
        key: detail["value_int"]
        for key, detail in _company_value_details(cycle, report_code).items()
    }


def company_trend(cycle, report_code, row_code):
    """Return ``{period: detail}`` for one row across all approved projects."""
    return {
        period: detail
        for (rc, period), detail in _company_value_details(cycle, report_code).items()
        if rc == row_code
    }


def project_trend_rows(cycle, report_code, row_code):
    """Return ``[{code, name, series}]`` where series is ``{period: detail}`` per project."""
    if cycle is None:
        return []
    pcs = list(
        ProjectCycle.objects.filter(
            cycle=cycle,
            current_upload__cycle=cycle,
            current_upload__project=F("project"),
            current_upload__status=UploadVersion.Status.APPROVED,
            project__is_active=True,
        )
        .select_related("project")
        .order_by("project__code")
    )
    upload_to_project = {pc.current_upload_id: pc.project for pc in pcs}
    per = defaultdict(lambda: defaultdict(list))
    for value in NormalizedValue.objects.filter(
        upload_id__in=list(upload_to_project),
        report_code=report_code,
        row_code=row_code,
    ):
        per[value.upload_id][value.period].append(value)
    rows = []
    for pc in pcs:
        series = {
            period: _detail_for_rows(vals)
            for period, vals in per.get(pc.current_upload_id, {}).items()
        }
        rows.append({"code": pc.project.code, "name": pc.project.name, "series": series})
    return rows


def sub_table_reports(cycle):
    template = active_template(cycle)
    specs = []
    if template and template.file_path:
        specs = sub_table_specs(settings.BASE_DIR / template.file_path)
    counts = {}
    if cycle:
        current_ids = ProjectCycle.objects.filter(
            cycle=cycle,
            current_upload__cycle=cycle,
            current_upload__project=F("project"),
            current_upload__status=UploadVersion.Status.APPROVED,
            project__is_active=True,
        ).values_list("current_upload_id", flat=True)
        counts = dict(
            NormalizedValue.objects.filter(upload_id__in=current_ids)
            .exclude(report_code__in=REPORTS)
            .values("report_code")
            .annotate(n=Count("row_code", distinct=True))
            .values_list("report_code", "n")
        )
    return [
        {"code": code, "name": name, "rows": counts.get(code, 0)}
        for code, name in specs
    ]


def project_contributions(cycle, report_code, row_code=None, period=None):
    current_ids = ProjectCycle.objects.filter(
        cycle=cycle,
        current_upload__cycle=cycle,
        current_upload__project=F("project"),
        current_upload__status=UploadVersion.Status.APPROVED,
        project__is_active=True,
    ).values_list("current_upload_id", flat=True)
    values = NormalizedValue.objects.filter(upload_id__in=current_ids, report_code=report_code)
    if row_code:
        values = values.filter(row_code=row_code)
    if period:
        values = values.filter(period=period)
    return values.select_related("upload", "upload__project").order_by("upload__project__code", "row_code", "period")


@transaction.atomic
def create_adjustment_batch(cycle, report_code, row_code, period, delta_cents, reason, actor=None, due_date=None):
    cycle = _lock_mutable_cycle(cycle)
    baselines = {}
    for pc in ProjectCycle.objects.filter(cycle=cycle, current_upload__status=UploadVersion.Status.APPROVED).select_related("project", "current_upload"):
        value = NormalizedValue.objects.filter(
            upload=pc.current_upload,
            report_code=report_code,
            row_code=row_code,
            period=period,
            unit=NormalizedValue.Unit.MONEY,
        ).first()
        baselines[pc.project.code] = int(value.value_int or 0) if value else 0
    if not baselines:
        raise ValueError("没有可用于调整分配的已批准项目版本")
    batch = AdjustmentBatch.objects.create(
        cycle=cycle,
        report_code=report_code,
        row_code=row_code,
        period=period,
        baseline_total_cents=sum(baselines.values()),
        delta_cents=delta_cents,
        reason=reason,
        due_date=due_date,
    )
    allocation = dict(largest_remainder(delta_cents, baselines.items()))
    projects = {project.code: project for project in Project.objects.filter(code__in=baselines)}
    for code, baseline in baselines.items():
        delta = allocation.get(code, 0)
        AdjustmentLine.objects.create(
            batch=batch,
            cycle=cycle,
            project=projects[code],
            report_code=report_code,
            row_code=row_code,
            period=period,
            baseline_cents=baseline,
            weight=abs(baseline),
            allocated_delta_cents=delta,
            target_cents=baseline + delta,
        )
    audit(actor, "ADJUSTMENT_DRAFTED", "AdjustmentBatch", batch.id, {"delta_cents": delta_cents}, cycle=cycle)
    return batch


@transaction.atomic
def create_driver_adjustment(cycle, project, report_code, driver, to_value, reason, actor=None, due_date=None):
    """Create a single-project driver adjustment and a matching R0041 line."""
    cycle = _lock_mutable_cycle(cycle)
    details = project_value_details(project, cycle, report_code)
    baseline = {rc: detail for (rc, period), detail in details.items() if period == "YEAR"}
    if not baseline:
        raise ValueError("该项目尚无已批准年度数据，无法测算")
    snapshot = simulate_driver(baseline, driver, to_value)
    rows = {r[0]: r for r in snapshot["rows"]}
    room_rev_baseline = rows["ROOM_REV"][3]
    room_rev_target = rows["ROOM_REV"][4]
    delta = snapshot["delta_room_rev"]
    batch = AdjustmentBatch.objects.create(
        cycle=cycle,
        project=project,
        driver=driver,
        driver_label=snapshot["driver_label"],
        from_value=Decimal(str(snapshot["from_value"])) if snapshot["from_value"] is not None else None,
        to_value=Decimal(str(snapshot["to_value"])),
        target_room_rev_cents=room_rev_target,
        cascade={
            "driver": snapshot["driver"],
            "driver_label": snapshot["driver_label"],
            "from_value": snapshot["from_value"],
            "to_value": snapshot["to_value"],
            "delta_room_rev": delta,
            "rows": [list(r) for r in snapshot["rows"]],
        },
        report_code=report_code,
        row_code=ROOM_REV,
        period="YEAR",
        baseline_total_cents=room_rev_baseline,
        delta_cents=delta,
        reason=reason,
        due_date=due_date,
    )
    AdjustmentLine.objects.create(
        batch=batch,
        cycle=cycle,
        project=project,
        report_code=report_code,
        row_code=ROOM_REV,
        period="YEAR",
        baseline_cents=room_rev_baseline,
        weight=1,
        allocated_delta_cents=delta,
        target_cents=room_rev_target,
    )
    audit(actor, "ADJUSTMENT_DRAFTED", "AdjustmentBatch", batch.id,
          {"driver": driver, "to_value": str(to_value), "delta_cents": delta},
          project=project, cycle=cycle)
    return batch


def _detail_numeric(detail):
    if detail.get("ratio_den"):
        return detail["ratio_num"] / detail["ratio_den"]
    return detail.get("value_int", 0)


def _stored_int(unit, value):
    if unit == "RATIO":
        return int(round(float(value) * RATIO_SCALE))
    return int(round(value))


def _cascade_value(unit, value):
    if unit == "RATIO":
        return float(value)
    return int(round(value))


def _project_report(project, cycle, report_code):
    """Baseline values + formula graph for one project report."""
    details = project_value_details(project, cycle, report_code)
    manifest = load_manifest()
    if not manifest.get("reports"):
        fallback_path = Path(settings.BASE_DIR) / "artifacts" / "template_manifest.json"
        if fallback_path.exists():
            manifest = json.loads(fallback_path.read_text(encoding="utf-8"))
    rows, num_to_code = build_report_graph(manifest, report_code)
    values = {}
    for (rc, period), detail in details.items():
        if rc in rows:
            values.setdefault(rc, {})[period] = _detail_numeric(detail)
    order = sorted(rows, key=lambda rc: rows[rc]["row_num"])
    return rows, num_to_code, values, order


def _validate_edits(rows, edits):
    for rc, per in edits.items():
        if rc not in rows:
            raise ValueError("未知行代码：" + rc)
        if rows[rc]["kind"] != "leaf":
            raise ValueError("该行为联动计算项，不可直接编辑：" + rows[rc]["label"])
        for period in per:
            if period != "YEAR" and period not in MONTHS:
                raise ValueError("未知期间：" + period)


def preview_adjustment(project, cycle, report_code, edits=None):
    """Recompute a project report with leaf edits; returns before/after rows.

    ``edits``: ``{row_code: {period: engine_numeric}}`` (MONEY cents, COUNT int,
    RATIO fraction).  Leaf-only; derived rows are recomputed.
    """
    edits = edits or {}
    rows, num_to_code, values, order = _project_report(project, cycle, report_code)
    _validate_edits(rows, edits)
    result = recompute(rows, num_to_code, values, edits)
    return {
        "report_code": report_code,
        "periods": MONTHS + ["YEAR"],
        "rows": [
            {
                "code": rc,
                "label": rows[rc]["label"],
                "unit": rows[rc]["unit"],
                "kind": rows[rc]["kind"],
                "before": values.get(rc, {}),
                "after": result.get(rc, {}),
            }
            for rc in order
        ],
        "edited": {rc: per for rc, per in edits.items()},
    }


@transaction.atomic
def create_full_adjustment(cycle, project, report_code, edits, reason, actor=None, due_date=None, batch=None):
    """Issue a full-table adjustment: one batch + one line per edited leaf."""
    cycle = _lock_mutable_cycle(cycle)
    rows, num_to_code, values, order = _project_report(project, cycle, report_code)
    _validate_edits(rows, edits)
    if not edits:
        raise ValueError("没有可下发的调整项")
    for rc, per in edits.items():
        for period in per:
            if AdjustmentLine.objects.filter(
                cycle=cycle,
                project=project,
                report_code=report_code,
                row_code=rc,
                period=period,
                status=AdjustmentLine.Status.OPEN,
            ).exists():
                raise ValueError(f"「{rows[rc]['label']}」{period} 已有未确认的调整任务，请先取消或完成")
    result = recompute(rows, num_to_code, values, edits)
    cascade_rows = [
        [
            rc,
            rows[rc]["label"],
            rows[rc]["unit"],
            _cascade_value(rows[rc]["unit"], values.get(rc, {}).get("YEAR", 0)),
            _cascade_value(rows[rc]["unit"], result.get(rc, {}).get("YEAR", 0)),
        ]
        for rc in order
    ]
    edited_rows = [
        [
            rc,
            rows[rc]["label"],
            rows[rc]["unit"],
            period,
            _cascade_value(rows[rc]["unit"], values.get(rc, {}).get(period, 0)),
            _cascade_value(rows[rc]["unit"], value),
        ]
        for rc, per in sorted(edits.items())
        for period, value in sorted(per.items())
    ]
    line_data = []
    total_delta = 0
    for rc, per in edits.items():
        unit = rows[rc]["unit"]
        for period, value in per.items():
            baseline_int = _stored_int(unit, values.get(rc, {}).get(period, 0))
            target_int = _stored_int(unit, value)
            line_data.append((rc, period, baseline_int, target_int))
            total_delta += target_int - baseline_int
    if batch is None:
        target_room_rev = int(round(result.get("R0041", {}).get("YEAR", 0))) if "R0041" in result else 0
        batch = AdjustmentBatch.objects.create(
            cycle=cycle,
            project=project,
            driver="",
            driver_label="全表调整",
            target_room_rev_cents=target_room_rev,
            cascade={
                "kind": "full",
                "report_code": report_code,
                "rows": cascade_rows,
                "edited": edited_rows,
                "reports": {
                    report_code: {"rows": cascade_rows, "edited": edited_rows},
                },
            },
            report_code=report_code,
            row_code="",
            period="",
            baseline_total_cents=0,
            delta_cents=total_delta,
            reason=reason,
            due_date=due_date,
        )
    else:
        batch = AdjustmentBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.cycle_id != cycle.pk or batch.project_id != project.pk:
            raise ValueError("追加全表调整的项目或预算周期不一致")
        if batch.status != AdjustmentBatch.Status.DRAFT:
            raise ValueError("只有草稿调整可以追加全表变更")
        cascade = dict(batch.cascade or {})
        reports = dict(cascade.get("reports") or {})
        reports[report_code] = {"rows": cascade_rows, "edited": edited_rows}
        cascade["reports"] = reports
        batch.cascade = cascade
        batch.delta_cents += total_delta
        batch.save(update_fields=["cascade", "delta_cents"])
    for rc, period, baseline_int, target_int in line_data:
        AdjustmentLine.objects.create(
            batch=batch,
            cycle=cycle,
            project=project,
            report_code=report_code,
            row_code=rc,
            period=period,
            baseline_cents=baseline_int,
            weight=1,
            allocated_delta_cents=target_int - baseline_int,
            target_cents=target_int,
        )
    audit(
        actor,
        "ADJUSTMENT_DRAFTED",
        "AdjustmentBatch",
        batch.id,
        {"kind": "full", "report_code": report_code, "edited": sorted(edits), "delta_cents": total_delta},
        project=project,
        cycle=cycle,
    )
    return batch


@transaction.atomic
def override_adjustment_line(line, allocated_delta_cents, actor=None):
    line = AdjustmentLine.objects.select_for_update().get(pk=line.pk)
    _lock_mutable_cycle(line.cycle)
    if line.batch.status != AdjustmentBatch.Status.DRAFT:
        raise ValueError("只有草稿调整可以手工覆盖")
    line.allocated_delta_cents = allocated_delta_cents
    line.target_cents = line.baseline_cents + allocated_delta_cents
    line.save(update_fields=["allocated_delta_cents", "target_cents"])
    audit(actor, "ADJUSTMENT_LINE_OVERRIDDEN", "AdjustmentLine", line.id, {"allocated_delta_cents": allocated_delta_cents}, project=line.project, cycle=line.cycle)
    return line


@transaction.atomic
def issue_adjustment(batch, actor=None):
    batch = AdjustmentBatch.objects.select_for_update().get(pk=batch.pk)
    batch.cycle = _lock_mutable_cycle(batch.cycle)
    if batch.status != AdjustmentBatch.Status.DRAFT:
        raise ValueError("只有草稿调整可以下发")
    if (batch.cascade or {}).get("kind") == "fixed_cost_scenario":
        expected = batch.cascade.get("report_deltas_cents", {})
        actual = {code: 0 for code in expected}
        for line in batch.lines.all():
            actual[line.report_code] = actual.get(line.report_code, 0) + line.allocated_delta_cents
            if line.target_cents - line.baseline_cents != line.allocated_delta_cents:
                raise ValueError("场景目标与明细差额不一致")
        if not expected or actual != expected or actual.get(batch.report_code) != batch.delta_cents:
            raise ValueError("场景各报表差额必须与测算结果逐分一致")
    elif sum(line.allocated_delta_cents for line in batch.lines.all()) != batch.delta_cents:
        raise ValueError("项目差额合计必须精确等于批次差额")
    batch.status = AdjustmentBatch.Status.ISSUED
    batch.issued_at = timezone.now()
    batch.save(update_fields=["status", "issued_at"])
    batch.cycle.status = BudgetCycle.Status.ADJUSTING
    batch.cycle.save(update_fields=["status"])
    audit(actor, "ADJUSTMENT_ISSUED", "AdjustmentBatch", batch.id, cycle=batch.cycle)
    return batch


@transaction.atomic
def cancel_adjustment(batch, actor=None):
    batch = AdjustmentBatch.objects.select_for_update().get(pk=batch.pk)
    batch.cycle = _lock_mutable_cycle(batch.cycle)
    if batch.status == AdjustmentBatch.Status.COMPLETED or batch.lines.filter(status=AdjustmentLine.Status.CONFIRMED).exists():
        raise ValueError("已有项目版本确认后不能直接取消")
    batch.status = AdjustmentBatch.Status.CANCELLED
    batch.save(update_fields=["status"])
    batch.lines.filter(status=AdjustmentLine.Status.OPEN).update(status=AdjustmentLine.Status.CANCELLED)
    audit(actor, "ADJUSTMENT_CANCELLED", "AdjustmentBatch", batch.id, cycle=batch.cycle)
    return batch


@transaction.atomic
def update_adjustment_lines_for_upload(upload):
    cycle = _lock_mutable_cycle(upload.cycle)
    open_lines = AdjustmentLine.objects.filter(project=upload.project, cycle=cycle, status=AdjustmentLine.Status.OPEN, batch__status=AdjustmentBatch.Status.ISSUED)
    for line in open_lines:
        value = NormalizedValue.objects.filter(upload=upload, report_code=line.report_code, row_code=line.row_code, period=line.period).first()
        line.latest_upload = upload
        line.latest_value_cents = int(value.value_int or 0) if value else None
        line.difference_cents = None if line.latest_value_cents is None else line.latest_value_cents - line.target_cents
        if line.difference_cents == 0 and upload.status == UploadVersion.Status.APPROVED:
            line.status = AdjustmentLine.Status.CONFIRMED
        line.save(update_fields=["latest_upload", "latest_value_cents", "difference_cents", "status"])
    for batch in AdjustmentBatch.objects.filter(lines__project=upload.project, cycle=upload.cycle, status=AdjustmentBatch.Status.ISSUED).distinct():
        if not batch.lines.filter(status=AdjustmentLine.Status.OPEN).exists():
            batch.status = AdjustmentBatch.Status.COMPLETED
            batch.save(update_fields=["status"])


update_adjustment_lines = update_adjustment_lines_for_upload


def freeze_preconditions(cycle):
    blockers = []
    if BudgetCycle.objects.filter(pk=cycle.pk, status=BudgetCycle.Status.FROZEN).exists():
        return ["周期已冻结"]
    for project in Project.objects.filter(is_active=True).order_by("code"):
        if not ProjectCycle.objects.filter(project=project, cycle=cycle, current_upload__status=UploadVersion.Status.APPROVED).exists():
            blockers.append(f"{project.code} 无正式版本")
    current_ids = ProjectCycle.objects.filter(cycle=cycle, current_upload__status=UploadVersion.Status.APPROVED).values_list("current_upload_id", flat=True)
    if ValidationIssue.objects.filter(run__upload_id__in=current_ids, severity=ValidationIssue.Severity.P0).exists():
        blockers.append("存在 P0 阻断问题")
    if any(not issue.acknowledged or not issue.acknowledgement_note.strip() for issue in ValidationIssue.objects.filter(run__upload_id__in=current_ids, severity=ValidationIssue.Severity.P1)):
        blockers.append("存在未确认 P1 问题")
    if AdjustmentLine.objects.filter(cycle=cycle, status=AdjustmentLine.Status.OPEN).exists():
        blockers.append("存在未完成调整")
    return blockers


def freeze_cycle(cycle, actor=None):
    blockers = freeze_preconditions(cycle)
    if blockers:
        raise ValueError("；".join(blockers))
    snapshot = FreezeSnapshot.objects.create(cycle=cycle, status=FreezeSnapshot.Status.STAGING)
    tmp = settings.BUDGET_STORAGE_ROOT / f"{snapshot.id}.tmp"
    tmp_zip = settings.BUDGET_STORAGE_ROOT / f"{snapshot.id}.tmp.zip"
    final = settings.BUDGET_STORAGE_ROOT / str(snapshot.id)
    zip_path = settings.BUDGET_STORAGE_ROOT / f"{snapshot.id}.zip"
    try:
        tmp.mkdir(parents=True, exist_ok=False)
        _copy_project_originals(cycle, tmp / "项目原件")
        _write_company_report_xlsx(cycle, tmp / "四表汇总.xlsx")
        (tmp / "四表汇总.json").write_text(
            json.dumps(_company_report_payload(cycle), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _write_adjustment_ledger_xlsx(cycle, tmp / "调整台账.xlsx")
        _write_validation_report_xlsx(cycle, tmp / "校验报告.xlsx")
        manifest = _file_manifest(tmp)
        (tmp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if final.exists() or zip_path.exists() or tmp_zip.exists():
            raise ValueError("快照文件已存在")
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(tmp.rglob("*")):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(tmp))
        tmp.replace(final)
        tmp_zip.replace(zip_path)
        with transaction.atomic():
            cycle = BudgetCycle.objects.select_for_update().get(pk=cycle.pk)
            if cycle.status == BudgetCycle.Status.FROZEN:
                raise ValueError("周期已冻结")
            if freeze_preconditions(cycle):
                raise ValueError("冻结提交前版本状态发生变化")
            snapshot.directory = str(final.relative_to(settings.BUDGET_STORAGE_ROOT))
            snapshot.manifest_path = str((final / "manifest.json").relative_to(settings.BUDGET_STORAGE_ROOT))
            snapshot.status = FreezeSnapshot.Status.COMPLETE
            snapshot.completed_at = timezone.now()
            snapshot.save(update_fields=["directory", "manifest_path", "status", "completed_at"])
            for relative, digest in manifest.items():
                path = final / relative
                SnapshotArtifact.objects.create(
                    snapshot=snapshot,
                    kind=path.suffix.lstrip(".") or "file",
                    relative_path=str(path.relative_to(settings.BUDGET_STORAGE_ROOT)),
                    sha256=digest,
                    size=path.stat().st_size,
                )
            manifest_path = final / "manifest.json"
            SnapshotArtifact.objects.create(
                snapshot=snapshot,
                kind="json",
                relative_path=str(manifest_path.relative_to(settings.BUDGET_STORAGE_ROOT)),
                sha256=sha256_file(manifest_path),
                size=manifest_path.stat().st_size,
            )
            SnapshotArtifact.objects.create(
                snapshot=snapshot,
                kind="zip",
                relative_path=str(zip_path.relative_to(settings.BUDGET_STORAGE_ROOT)),
                sha256=sha256_file(zip_path),
                size=zip_path.stat().st_size,
            )
            cycle.status = BudgetCycle.Status.FROZEN
            cycle.frozen_at = timezone.now()
            cycle.save(update_fields=["status", "frozen_at"])
            audit(actor, "CYCLE_FROZEN", "BudgetCycle", cycle.id, {"snapshot": str(snapshot.id)}, cycle=cycle)
    except Exception as exc:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        if tmp_zip.exists():
            tmp_zip.unlink(missing_ok=True)
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        if zip_path.exists():
            zip_path.unlink(missing_ok=True)
        snapshot.status = FreezeSnapshot.Status.FAILED
        snapshot.error = str(exc)
        snapshot.save(update_fields=["status", "error"])
        raise
    return snapshot


@transaction.atomic
def reopen_cycle(cycle, actor=None, project_ids=None):
    cycle = BudgetCycle.objects.select_for_update().get(pk=cycle.pk)
    later = BudgetCycle.objects.filter(
        budget_year=cycle.budget_year, revision_no=cycle.revision_no + 1
    ).exists()
    if later:
        raise ValueError("下一修订版本已存在，请勿重复开放")
    new_cycle = BudgetCycle.objects.create(
        name=cycle.name,
        budget_year=cycle.budget_year,
        revision_no=cycle.revision_no + 1,
        status=BudgetCycle.Status.OPEN,
        template=cycle.template,
        p1_threshold_cents=cycle.p1_threshold_cents,
    )
    qs = ProjectCycle.objects.filter(cycle=cycle).select_related("project", "current_upload")
    open_ids = set(project_ids or [])
    for pc in qs:
        src = pc.current_upload
        carry = src if src and src.status == UploadVersion.Status.APPROVED else None
        current_upload = _copy_reopen_baseline(carry, new_cycle, actor) if carry else None
        ProjectCycle.objects.create(
            project=pc.project,
            cycle=new_cycle,
            current_upload=current_upload,
            is_open=(pc.project_id in open_ids) or current_upload is None,
        )
    audit(actor, "CYCLE_REOPENED", "BudgetCycle", new_cycle.id, {"source_cycle": cycle.id}, cycle=new_cycle)
    return new_cycle


def _copy_reopen_baseline(source, new_cycle, actor=None):
    copied = UploadVersion.objects.create(
        project=source.project,
        cycle=new_cycle,
        template=source.template,
        status=UploadVersion.Status.APPROVED,
        original_name=source.original_name,
        original_path=source.original_path,
        recalculated_path=source.recalculated_path,
        sha256=source.sha256,
        note=f"由冻结周期 {source.cycle_id} 的版本 {source.id} 复制为修订基线",
        submitted_at=source.submitted_at,
        approved_at=source.approved_at,
    )
    NormalizedValue.objects.bulk_create(
        [
            NormalizedValue(
                upload=copied,
                report_code=value.report_code,
                row_code=value.row_code,
                row_label=value.row_label,
                period=value.period,
                data_year=value.data_year,
                data_kind=value.data_kind,
                month=value.month,
                unit=value.unit,
                value_int=value.value_int,
                ratio_num=value.ratio_num,
                ratio_den=value.ratio_den,
                source_sheet=value.source_sheet,
                source_cell=value.source_cell,
                source_formula=value.source_formula,
            )
            for value in NormalizedValue.objects.filter(upload=source)
        ]
    )
    for source_run in ValidationRun.objects.filter(upload=source):
        copied_run = ValidationRun.objects.create(
            upload=copied,
            rule_version=source_run.rule_version,
            passed=source_run.passed,
        )
        ValidationIssue.objects.bulk_create(
            [
                ValidationIssue(
                    run=copied_run,
                    severity=issue.severity,
                    code=issue.code,
                    message=issue.message,
                    location=issue.location,
                    actual_value=issue.actual_value,
                    expected_value=issue.expected_value,
                    acknowledged=issue.acknowledged,
                    acknowledgement_note=issue.acknowledgement_note,
                )
                for issue in source_run.issues.all()
            ]
        )
    audit(
        actor,
        "UPLOAD_BASELINE_COPIED",
        "UploadVersion",
        copied.id,
        {"source_upload": str(source.id), "source_cycle": source.cycle_id},
        project=source.project,
        cycle=new_cycle,
        upload=copied,
    )
    return copied


def _copy_project_originals(cycle, target_dir):
    target_dir.mkdir()
    for pc in ProjectCycle.objects.filter(cycle=cycle).select_related("project", "current_upload"):
        if pc.current_upload:
            source = settings.BUDGET_STORAGE_ROOT / pc.current_upload.original_path
            _verify_original_hash(pc.current_upload, source)
            shutil.copy2(source, target_dir / f"{pc.project.code}_{source.name}")


def _record_formula_mapping_issues(run, upload, source_path):
    if not upload.template or not upload.template.manifest_path:
        return
    manifest_path = Path(upload.template.manifest_path)
    if not manifest_path.is_absolute():
        manifest_path = Path(settings.BASE_DIR) / manifest_path
    template_path = Path(upload.template.file_path)
    if not template_path.is_absolute():
        template_path = Path(settings.BASE_DIR) / template_path
    if not manifest_path.exists() or not template_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        uploaded_rows, _ = formula_manifest(source_path)
        template_rows, _ = formula_manifest(template_path)
    except Exception:
        return
    uploaded_formulas = {
        (row["sheet"], row["cell"]): row for row in uploaded_rows
    }
    template_formulas = {
        (row["sheet"], row["cell"]): row for row in template_rows
    }
    for report_code, default_sheet in REPORTS.items():
        report = (manifest.get("reports") or {}).get(report_code) or {}
        sheet = report.get("sheet") or default_sheet
        for item in report.get("mapping") or report.get("cells") or []:
            cell_ref = item.get("cell") or item.get("source_cell")
            if not cell_ref:
                continue
            actual = uploaded_formulas.get((sheet, cell_ref))
            expected = template_formulas.get((sheet, cell_ref))
            if actual != expected and (actual is not None or expected is not None):
                ValidationIssue.objects.get_or_create(
                    run=run,
                    code="FORMULA_CHANGED",
                    location=f"{sheet}!{cell_ref}",
                    defaults={
                        "severity": ValidationIssue.Severity.P0,
                        "message": "映射单元格公式与平台模板不一致。",
                        "actual_value": json.dumps(
                            actual or {"formula": ""},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        "expected_value": json.dumps(
                            expected or {"formula": ""},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    },
                )


def _write_company_report_xlsx(cycle, path):
    wb = Workbook()
    first = True
    for report_code, title in REPORTS.items():
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = report_code[:31]
        ws.append([title])
        ws.append(["row_code", "period", "unit", "value_int", "ratio_num", "ratio_den"])
        for (row_code, period), detail in sorted(_company_value_details(cycle, report_code).items()):
            ws.append([
                row_code,
                period,
                detail["unit"],
                detail["value_int"],
                detail["ratio_num"],
                detail["ratio_den"],
            ])
    wb.save(path)


def _company_report_payload(cycle):
    payload = {}
    for report_code in REPORTS:
        payload[report_code] = {
            f"{row_code}|{period}": {
                "unit": detail["unit"],
                "value_int": detail["value_int"],
                "ratio_num": detail["ratio_num"],
                "ratio_den": detail["ratio_den"],
            }
            for (row_code, period), detail in sorted(
                _company_value_details(cycle, report_code).items()
            )
        }
    return payload


def _write_adjustment_ledger_xlsx(cycle, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "调整台账"
    ws.append(["batch", "project", "report_code", "row_code", "period", "baseline_cents", "allocated_delta_cents", "target_cents", "latest_value_cents", "difference_cents", "status"])
    for line in AdjustmentLine.objects.filter(cycle=cycle).select_related("batch", "project").order_by("batch_id", "project__code"):
        ws.append([line.batch_id, line.project.code, line.report_code, line.row_code, line.period, line.baseline_cents, line.allocated_delta_cents, line.target_cents, line.latest_value_cents, line.difference_cents, line.status])
    wb.save(path)


def _write_validation_report_xlsx(cycle, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "校验报告"
    ws.append(["upload", "severity", "code", "message", "location", "actual_value", "expected_value", "acknowledged"])
    for issue in ValidationIssue.objects.filter(run__upload__cycle=cycle).select_related("run").order_by("id"):
        ws.append([str(issue.run.upload_id), issue.severity, issue.code, issue.message, issue.location, issue.actual_value, issue.expected_value, issue.acknowledged])
    wb.save(path)


def _file_manifest(root):
    manifest = {}
    for file_path in sorted(root.rglob("*")):
        if file_path.is_file() and file_path.name != "manifest.json":
            manifest[str(file_path.relative_to(root))] = sha256_file(file_path)
    return manifest
