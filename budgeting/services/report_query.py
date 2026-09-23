"""One batched, explicit-context read path; missing values never become zero."""
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path

from django.conf import settings

from budgeting.models import BudgetCycle, FreezeSnapshot, NormalizedValue, Project, PlanProject, SnapshotArtifact, UploadVersion
from .metric_definitions import load_definitions, verified_legacy_mapping
from .report_context import ReportContext


@dataclass(frozen=True)
class MetricResult:
    row_code: str
    label: str
    unit: str
    value: Decimal | None
    by_project: dict
    missing: dict
    numerator: int | None = None
    denominator: int | None = None


@dataclass(frozen=True)
class ReportResult:
    context: ReportContext
    metrics: tuple[MetricResult, ...]
    upload_ids: dict
    missing_projects: dict
    project_names: dict
    identity: str

    @property
    def coverage(self):
        return {"available": len(self.upload_ids), "expected": len(self.context.project_ids)}


def _frozen_selection(context):
    snapshot = FreezeSnapshot.objects.get(pk=context.snapshot_id, cycle_id=context.cycle_id,
                                          status=FreezeSnapshot.Status.COMPLETE)
    artifact = SnapshotArtifact.objects.get(snapshot=snapshot,
        relative_path=str(Path(snapshot.directory) / "report-selection.json"))
    root = Path(settings.BUDGET_STORAGE_ROOT).resolve()
    path = (root / artifact.relative_path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("快照路径越界。")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise ValueError("冻结集合文件校验失败。")
    payload = json.loads(raw)
    if payload["budget_year"] != context.budget_year or payload["cycle_id"] != context.cycle_id:
        raise ValueError("冻结集合年度或轮次不符。")
    if not set(context.project_ids).issubset(set(map(int, payload["project_ids"]))):
        raise ValueError("项目不属于冻结集合。")
    return payload


def query_report(context: ReportContext, *, _prepared=None) -> ReportResult:
    prepared = _prepared if _prepared is not None else {}
    if prepared:
        cycle, projects, definitions, manifest_hash, frozen, selected, missing, all_values = prepared["data"]
    else:
        return _prepare_and_query(context, prepared)
    return _calculate(context, cycle, projects, definitions, manifest_hash, frozen, selected, missing, all_values)


def _prepare_and_query(context, prepared):
    cycle = BudgetCycle.objects.select_related("template").get(pk=context.cycle_id)
    if cycle.budget_year != context.budget_year:
        raise ValueError("年度与轮次不一致。")
    projects = dict(Project.objects.filter(pk__in=context.project_ids).values_list("pk", "name"))
    if set(projects) != set(context.project_ids):
        raise ValueError("存在无效项目。")
    if cycle.plan_id and not set(context.project_ids).issubset(set(PlanProject.objects.filter(plan_id=cycle.plan_id).values_list("project_id", flat=True))):
        raise ValueError("项目不属于年度计划固定集合。")
    definitions, manifest_hash = load_definitions(cycle.template, context.report_code)
    frozen = _frozen_selection(context) if context.source_mode == "FROZEN" else None
    uploads = UploadVersion.objects.filter(cycle=cycle, project_id__in=context.project_ids).select_related("template")
    if frozen:
        if frozen["manifest_hash"] != manifest_hash:
            raise ValueError("冻结模板规则指纹已改变。")
        uploads = uploads.filter(pk__in=frozen["upload_ids"].values())
    elif context.source_mode == "APPROVED":
        uploads = uploads.filter(status=UploadVersion.Status.APPROVED)
    selected, missing = {}, {}
    for upload in uploads.order_by("project_id", "-created_at", "-id"):
        selected.setdefault(upload.project_id, upload)
    legacy_mappings = {}
    usable = {UploadVersion.Status.VALIDATED, UploadVersion.Status.SUBMITTED, UploadVersion.Status.APPROVED}
    for project_id in context.project_ids:
        upload = selected.get(project_id)
        reason = None
        if upload is None:
            reason = "NOT_UPLOADED" if context.source_mode == "WORKING" else "NO_APPROVED_UPLOAD"
        elif upload.template_id != cycle.template_id:
            mapping = verified_legacy_mapping(upload, cycle, context.report_code, definitions)
            if mapping is None:
                reason = "TEMPLATE_MISMATCH"
            else:
                legacy_mappings[upload.pk] = mapping
        if upload is not None and not reason and frozen and str(upload.pk) != frozen["upload_ids"].get(str(project_id)):
            raise ValueError("冻结项目与上传归属不一致。")
        elif upload is not None and not reason and not frozen and upload.status not in usable:
            reason = "PROCESSING" if upload.status in {"RECEIVED", "PROCESSING"} else "REJECTED"
        if reason:
            missing[project_id] = reason
            selected.pop(project_id, None)
    if frozen and set(selected) != set(context.project_ids):
        raise ValueError("冻结上传集合不完整，禁止回退到当前数据。")
    all_values = list(NormalizedValue.objects.filter(upload_id__in=[u.pk for u in selected.values()],
        report_code=context.report_code))
    if legacy_mappings:
        checked = []
        for value in all_values:
            legacy = legacy_mappings.get(value.upload_id)
            if legacy is None:
                checked.append(value)
                continue
            period = 'YEAR' if value.month is None else f'{value.month:02}'
            expected = legacy['cells'].get((value.row_code, period))
            if (value.data_kind != 'BUDGET' or value.data_year != cycle.budget_year or expected is None
                    or (value.source_sheet,value.source_cell,value.row_label) != expected):
                continue
            checked.append(value)
        all_values = checked
        cell_lookup = {(v.upload_id,v.row_code,v.period):v for v in all_values}
        for value in all_values:
            if value.upload_id not in legacy_mappings:
                continue
            definition = definitions.get(value.row_code)
            if definition and definition.aggregation == 'WEIGHTED':
                numerator = cell_lookup.get((value.upload_id,definition.numerator_row,value.period))
                denominator = cell_lookup.get((value.upload_id,definition.denominator_row,value.period))
                value.ratio_num = numerator.value_int if numerator else None
                value.ratio_den = denominator.value_int if denominator else None
        manifest_hash = hashlib.sha256(json.dumps({'canonical':manifest_hash,'legacy':{
            str(key):entry['manifest_hash'] for key,entry in legacy_mappings.items()}},sort_keys=True).encode()).hexdigest()
    prepared["data"] = (cycle, projects, definitions, manifest_hash, frozen, selected, missing, all_values)
    return query_report(context, _prepared=prepared)


def _calculate(context, cycle, projects, definitions, manifest_hash, frozen, selected, missing, all_values):
    month = None if context.period == "YEAR" else int(context.period)
    year = context.data_year or context.budget_year
    values = [v for v in all_values if v.data_year == year and v.data_kind == context.data_kind and v.month == month]
    by_upload = {u.pk: p for p, u in selected.items()}
    cells = {}
    for value in values:
        project_id = by_upload[value.upload_id]
        upload = selected[project_id]
        expected_run = frozen.get("processing_runs", {}).get(str(project_id)) if frozen else (str(upload.processing_current_run_id) if upload.processing_current_run_id else None)
        actual_run = str(value.processing_run_id) if value.processing_run_id else None
        if actual_run != expected_run:
            continue
        key = (project_id, value.row_code)
        if key in cells:
            raise ValueError(f"同一指标期间存在重复值：{value.row_code}")
        cells[key] = value
    results = []
    for code, definition in definitions.items():
        project_values, failures, numerators, denominators = {}, dict(missing), [], []
        for project_id in selected:
            cell = cells.get((project_id, code))
            if cell is None:
                failures[project_id] = "MISSING_VALUE"
                continue
            if cell.unit != definition.unit:
                failures[project_id] = "UNIT_MISMATCH"
                continue
            scale = Decimal(100) if definition.unit == "MONEY" else Decimal(1)
            if definition.aggregation == "WEIGHTED":
                numerator, denominator = cell.ratio_num, cell.ratio_den
                if numerator is None or denominator is None:
                    failures[project_id] = "MISSING_COMPONENTS"
                    continue
                if denominator < 0:
                    failures[project_id] = "INVALID_DENOMINATOR"
                    continue
                numerators.append(numerator)
                denominators.append(denominator)
                project_values[project_id] = Decimal(numerator) / Decimal(denominator) / scale if denominator else None
            elif definition.aggregation == "SUM":
                project_values[project_id] = Decimal(cell.value_int) / scale
            else:
                failures[project_id] = "UNKNOWN_RULE"
        numerator = sum(numerators) if numerators else None
        denominator = sum(denominators) if denominators else None
        if definition.aggregation == "WEIGHTED":
            value = Decimal(numerator) / Decimal(denominator) / (100 if definition.unit == "MONEY" else 1) if denominator else None
        else:
            value = sum(project_values.values(), Decimal(0)) if project_values else None
        # Available contributions remain visible; missing explicitly marks a partial total.
        if value is not None:
            value = value.quantize(Decimal(1).scaleb(-definition.precision), rounding=ROUND_HALF_UP)
        results.append(MetricResult(code, definition.label, definition.unit, value,
                                    project_values, failures, numerator, denominator))
    identity_payload = {"context": context.query_string(), "manifest": manifest_hash,
        "uploads": {str(p): [str(u.pk), u.sha256, str(u.processing_current_run_id)] for p, u in selected.items()},
        "missing": missing}
    identity = hashlib.sha256(json.dumps(identity_payload, sort_keys=True).encode()).hexdigest()
    return ReportResult(context, tuple(results), {p: str(u.pk) for p, u in selected.items()}, missing, projects, identity)


def freeze_selection_payload(context: ReportContext):
    """Call under the freeze writer lock, persist and register its SHA as SnapshotArtifact."""
    if context.source_mode != "APPROVED":
        raise ValueError("冻结集合必须来自正式稿。")
    result = query_report(context)
    if result.missing_projects:
        raise ValueError("正式稿项目集合不完整。")
    cycle = BudgetCycle.objects.select_related("template").get(pk=context.cycle_id)
    _, manifest_hash = load_definitions(cycle.template, context.report_code)
    runs = UploadVersion.objects.filter(pk__in=result.upload_ids.values()).values_list("project_id", "processing_current_run_id")
    return {"schema": 1, "budget_year": context.budget_year, "cycle_id": context.cycle_id,
            "project_ids": list(context.project_ids), "upload_ids": {str(p): u for p, u in result.upload_ids.items()},
            "manifest_hash": manifest_hash,
            "processing_runs": {str(p): str(r) if r else None for p, r in runs}}


def query_report_table(context, *, include_history=False):
    """All months plus annual totals share one batched read, without a process-global cache."""
    prepared, reports = {}, {}
    dimensions = [(context.budget_year, "BUDGET")]
    if include_history:
        dimensions = [(context.budget_year - 3, "ACTUAL"), (context.budget_year - 2, "ACTUAL"), (context.budget_year - 1, "FORECAST")] + dimensions
    for year, kind in dimensions:
        for period in [*[f"{m:02}" for m in range(1, 13)], "YEAR"]:
            selected_context = replace(context, data_year=year, data_kind=kind, period=period)
            key = period if kind == "BUDGET" else f"{kind[0]}{year}" + (f"M{period}" if period != "YEAR" else "")
            reports[key] = query_report(selected_context, _prepared=prepared)
    return reports
