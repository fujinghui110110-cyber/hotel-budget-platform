"""Annual membership and explicitly confirmed, append-only historical versions."""
import hashlib
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Max

from budgeting.models import (
    BudgetCycle, BudgetPlan, HistoryBaseline, HistoryBaselineValue,
    PlanHistoryBinding, PlanProject, Project, ProjectCycle, UploadVersion,
)

VALUE_FIELDS = ("report_code", "row_code", "data_year", "data_kind", "period", "unit",
                "value_int", "ratio_num", "ratio_den", "source_sheet", "source_cell")
KEY_FIELDS = VALUE_FIELDS[:5]


def _admin(actor):
    if not actor or not actor.is_active or not actor.is_admin_role:
        raise PermissionDenied("只有管理员可以确认或修订历史基准。")


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _key(value):
    return tuple(value[name] for name in KEY_FIELDS)


def _canonical(values):
    result, keys = [], set()
    for value in values:
        row = {name: value.get(name) for name in VALUE_FIELDS}
        for name in ("source_sheet", "source_cell"):
            row[name] = row[name] or ""
        if row["data_kind"] not in {"ACTUAL", "FORECAST"}:
            raise ValidationError("历史基准只接受实际和预测数据。")
        if not all(row[name] is not None for name in VALUE_FIELDS[:7]):
            raise ValidationError("历史基准维度和值不可缺失。")
        if type(row["value_int"]) is not int:
            raise ValidationError("历史金额必须使用整数分，禁止浮点转换。")
        if _key(row) in keys:
            raise ValidationError("历史数据存在重复维度，必须先处理冲突。")
        keys.add(_key(row))
        result.append(row)
    if not result:
        raise ValidationError("没有可确认的历史数据。")
    return sorted(result, key=_key)


@transaction.atomic
def ensure_plan(cycle):
    """Freeze known cycle membership; do not infer historical confirmation."""
    plan, created = BudgetPlan.objects.get_or_create(budget_year=cycle.budget_year)
    if created:
        ids = list(ProjectCycle.objects.filter(cycle__budget_year=cycle.budget_year)
                   .values_list("project_id", flat=True).distinct())
        # Legacy ProjectCycle rows were created lazily; include active members once,
        # then keep the annual roster independent of future activation changes.
        ids = sorted(set(ids) | set(Project.objects.filter(is_active=True).values_list("pk", flat=True)))
        PlanProject.objects.bulk_create([PlanProject(plan=plan, project_id=pk) for pk in ids])
    BudgetCycle.objects.filter(budget_year=cycle.budget_year, plan__isnull=True).update(plan=plan)
    cycle.plan = plan
    return plan


def current_binding(plan, project):
    return PlanHistoryBinding.objects.filter(plan=plan, project=project).order_by("-revision").first()


def assert_current_history(upload):
    if not upload.cycle.plan_id:
        return
    binding = current_binding(upload.cycle.plan, upload.project)
    if upload.history_stale or (binding and upload.history_binding_id != binding.pk):
        raise ValidationError("HISTORY_BASELINE_STALE：历史基准已更新，请重新校验并提交。")


@transaction.atomic
def confirm_history(*, plan, project, values, actor, reason, source_identity,
                    expected_revision, affected_plan_ids=None):
    """Explicit confirmation/revision; every affected annual plan must be acknowledged."""
    _admin(actor)
    if not reason.strip() or not source_identity:
        raise ValidationError("确认历史必须填写理由和可追溯的来源依据。")
    if not PlanProject.objects.filter(plan=plan, project=project).exists():
        raise ValidationError("项目不在该年度范围内。")
    canonical = _canonical(values)
    previous = HistoryBaseline.objects.filter(project=project).order_by("-revision").first()
    revision = previous.revision if previous else 0
    if expected_revision != revision:
        raise ValidationError("历史版本已变化，请刷新后确认。")
    linked = set(PlanHistoryBinding.objects.filter(project=project).values_list("plan_id", flat=True))
    required = linked | {plan.pk}
    acknowledged = set(affected_plan_ids or [plan.pk])
    if acknowledged != required:
        raise ValidationError("本次历史修订影响多个预算年度，必须明确确认全部受影响年度。")
    # SQLite obtains a write lock at this first conditional update, before creating revisions.
    token = BudgetPlan.objects.get(pk=plan.pk).revision_token
    if BudgetPlan.objects.filter(pk=plan.pk, revision_token=token).update(revision_token=F("revision_token") + 1) != 1:
        raise ValidationError("年度计划发生并发变化，请重试。")
    old = {_key(v): v for v in previous.values.values(*VALUE_FIELDS)} if previous else {}
    new = {_key(v): v for v in canonical}
    if old.keys() - new.keys():
        raise ValidationError("历史修订必须保留已有完整范围，不可删减已确认科目或期间。")
    differences = [{"key": list(k), "before": old.get(k), "after": new.get(k)}
                   for k in sorted(old.keys() | new.keys()) if old.get(k) != new.get(k)]
    baseline = HistoryBaseline.objects.create(project=project, revision=revision + 1,
        content_hash=_hash(canonical), source_identity=source_identity, reason=reason,
        confirmed_by=actor, supersedes=previous, differences=differences)
    HistoryBaselineValue.objects.bulk_create([HistoryBaselineValue(baseline=baseline, **v) for v in canonical])
    for plan_id in sorted(required):
        latest = PlanHistoryBinding.objects.filter(plan_id=plan_id, project=project).aggregate(v=Max("revision"))["v"] or 0
        PlanHistoryBinding.objects.create(plan_id=plan_id, project=project, baseline=baseline,
            revision=latest + 1, binding_hash=_hash({"plan": plan_id, "project": project.pk,
                                                  "revision": latest + 1, "content": baseline.content_hash}))
        if plan_id != plan.pk:
            BudgetPlan.objects.filter(pk=plan_id).update(revision_token=F("revision_token") + 1)
        UploadVersion.objects.filter(cycle__plan_id=plan_id, project=project).exclude(
            status__in=[UploadVersion.APPROVED, UploadVersion.SUPERSEDED]).update(history_stale=True)
    from budgeting.services.workflow import audit
    audit(actor, "HISTORY_BASELINE_CONFIRMED", "HistoryBaseline", baseline.pk,
          {"reason": reason, "content_hash": baseline.content_hash, "affected_plans": sorted(required),
           "difference_count": len(differences)})
    return baseline


def validate_history_values(binding, values):
    """Return missing/changed locked cells without editing the user's workbook."""
    actual = {_key(v): v for v in values}
    issues = []
    for expected in binding.baseline.values.values(*VALUE_FIELDS):
        observed = actual.get(_key(expected))
        compare = ("unit", "value_int", "ratio_num", "ratio_den")
        if observed is None or any(observed.get(f) != expected[f] for f in compare):
            issues.append({"code": "HISTORY_LOCK_VIOLATION", "key": list(_key(expected)),
                           "expected": expected, "actual": observed})
    return issues


def history_metadata(binding):
    return {"history_binding_id": binding.pk, "history_binding_hash": binding.binding_hash,
            "history_baseline_id": binding.baseline_id, "history_baseline_hash": binding.baseline.content_hash}


@transaction.atomic
def bind_existing_history(*, plan, project, baseline, actor, reason):
    """Explicitly reuse an already confirmed project history in a later budget year."""
    _admin(actor)
    if not reason.strip() or baseline.project_id != project.pk:
        raise ValidationError("关联历史须填写理由且项目必须一致。")
    if not PlanProject.objects.filter(plan=plan, project=project).exists():
        raise ValidationError("项目不在年度范围。")
    if current_binding(plan, project):
        raise ValidationError("该年度已有历史基准，请使用有痕修订。")
    latest = HistoryBaseline.objects.filter(project=project).order_by("-revision").first()
    if latest.pk != baseline.pk:
        raise ValidationError("请选择当前已确认历史版本。")
    BudgetPlan.objects.filter(pk=plan.pk).update(revision_token=F("revision_token") + 1)
    binding = PlanHistoryBinding.objects.create(plan=plan, project=project, baseline=baseline,
        revision=1, binding_hash=_hash({"plan": plan.pk, "project": project.pk,
                                      "revision": 1, "content": baseline.content_hash}))
    UploadVersion.objects.filter(cycle__plan=plan, project=project).exclude(
        status__in=[UploadVersion.APPROVED, UploadVersion.SUPERSEDED]).update(history_stale=True)
    from budgeting.services.workflow import audit
    audit(actor, "HISTORY_BASELINE_BOUND", "PlanHistoryBinding", binding.pk, {"reason": reason})
    return binding
