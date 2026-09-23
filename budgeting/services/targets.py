"""Administrator-confirmed annual targets, independently evaluated for every round."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F

from budgeting.models import (
    BudgetPlan, NormalizedValue, PlanProject, TargetSet, TargetConstraint,
    TargetEvaluation, TargetEvaluationItem, UploadVersion,
)
from budgeting.services.plan_history import current_binding, assert_current_history


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError('目标与实际金额必须使用整数分。')
    return value


def latest_targets(plan, project):
    return TargetSet.objects.filter(plan=plan, project=project).order_by('-revision').first()


def _canonical(rows):
    result, scopes = [], {}
    for row in rows:
        required = ('report_code', 'row_code', 'period', 'unit', 'metric_kind', 'comparator', 'evidence', 'rule_version')
        if any(not row.get(k) for k in required):
            raise ValidationError('科目方向、单位、规则版本和确认依据必须完整，UNKNOWN不能下发。')
        if row['unit'] != 'CNY_CENT':
            raise ValidationError('此金额目标接口只接受CNY_CENT，其他指标需独立受控缩放规则。')
        kind, comparator = row['metric_kind'], row['comparator']
        directions = {'REVENUE': 'GE', 'EXPENSE': 'LE', 'PROFIT': 'GE'}
        if kind not in {*directions, 'OTHER'} or comparator not in {'GE', 'LE', 'EQ'}:
            raise ValidationError('未知科目类型或比较方式。')
        if kind in directions and comparator != directions[kind]:
            raise ValidationError('收入和利润要求不低于目标，费用要求不高于目标。')
        sign = row.get('sign_multiplier')
        if isinstance(sign, bool) or sign not in (-1, 1):
            raise ValidationError('必须明确确认符号映射，费用不能取绝对值。')
        if kind != 'EXPENSE' and sign != 1:
            raise ValidationError('收入和利润按原始带符号金额比较。')
        target = _integer(row.get('target_int'))
        record = {k: row[k] for k in required}
        record.update(target_int=target, sign_multiplier=sign)
        scope = tuple(row[k] for k in ('report_code', 'row_code', 'period', 'unit'))
        bounds = scopes.setdefault(scope, {})
        if comparator in bounds:
            raise ValidationError('同一指标比较方式重复。')
        if bounds and (bounds['meta'] != (kind, sign)):
            raise ValidationError('同一指标的类型和符号定义冲突。')
        bounds['meta'] = (kind, sign)
        bounds[comparator] = target
        lower = bounds.get('GE', bounds.get('EQ'))
        upper = bounds.get('LE', bounds.get('EQ'))
        if lower is not None and upper is not None and lower > upper:
            raise ValidationError('同一指标上下限冲突。')
        if 'EQ' in bounds and ((lower is not None and bounds['EQ'] < lower) or (upper is not None and bounds['EQ'] > upper)):
            raise ValidationError('同一指标等值要求与上下限冲突。')
        result.append(record)
    return result


@transaction.atomic
def issue_targets(*, actor, plan, project, origin_cycle, selected_target_rows,
                  expected_plan_revision, reason, source=None, revoke=False):
    """Rows are the complete explicit confirmation list, never automatic changed_rows.

    Metric kind/sign/evidence are administrator-confirmed controlled definitions.
    Revision replaces the preceding full list; omission must be shown in UI diff.
    """
    if not actor or not actor.is_active or not actor.is_admin_role:
        raise PermissionDenied('只有管理员可以下发、修订或撤销目标。')
    if not reason.strip():
        raise ValidationError('必须填写修订或撤销理由。')
    if origin_cycle.plan_id != plan.pk or not PlanProject.objects.filter(plan=plan, project=project).exists():
        raise ValidationError('目标年度或项目范围不一致。')
    rows = _canonical(selected_target_rows)
    if revoke and rows or not revoke and not rows:
        raise ValidationError('撤销须显式指定并提交空清单；下发须选定指标。')
    # First statement is the write/CAS: SQLite readers cannot silently win a stale edit.
    if BudgetPlan.objects.filter(pk=plan.pk, revision_token=expected_plan_revision).update(revision_token=F('revision_token') + 1) != 1:
        raise ValidationError('计划已变化，请刷新后重新确认目标。')
    previous = latest_targets(plan, project)
    target_set = TargetSet.objects.create(
        plan=plan, project=project, origin_cycle=origin_cycle,
        revision=(previous.revision + 1 if previous else 1),
        issued_sequence=expected_plan_revision + 1, supersedes=previous,
        created_by=actor, reason=reason, source=source or {},
    )
    TargetConstraint.objects.bulk_create([TargetConstraint(target_set=target_set, **row) for row in rows])
    return target_set


def evaluation_context(upload):
    plan = upload.cycle.plan
    if not plan:
        raise ValidationError('预算版本尚未绑定年度计划。')
    if not PlanProject.objects.filter(plan=plan, project=upload.project).exists():
        raise ValidationError('项目不在年度计划范围。')
    plan.refresh_from_db()
    target_set = latest_targets(plan, upload.project)
    binding = current_binding(plan, upload.project)
    return {
        'plan_id': plan.pk, 'plan_revision': plan.revision_token,
        'target_set_id': target_set.pk if target_set else None,
        'history_binding_id': binding.pk if binding else None,
        'processing_run_id': str(upload.processing_current_run_id or ''),
        'formula_hash': upload.template.formula_manifest_hash if upload.template_id else '',
        'rule_version': upload.template.rule_version if upload.template_id else '',
        'upload_sha256': upload.sha256,
    }


@transaction.atomic
def evaluate_upload(upload, *, expected_context=None):
    upload = UploadVersion.objects.select_related('cycle__plan', 'template', 'project').get(pk=upload.pk)
    context = evaluation_context(upload)
    if expected_context is not None and context != expected_context:
        raise ValidationError('TARGET_EVALUATION_STALE：目标、历史或公式版本已变化。')
    assert_current_history(upload)
    target_set = TargetSet.objects.filter(pk=context['target_set_id']).first()
    values = NormalizedValue.objects.filter(upload=upload)
    if upload.processing_current_run_id:
        values = values.filter(processing_run_id=upload.processing_current_run_id)
    values = {(v.report_code, v.row_code, v.period): v for v in values}
    items, status = [], 'PASS'
    for constraint in target_set.constraints.all() if target_set else []:
        row = values.get((constraint.report_code, constraint.row_code, constraint.period))
        result = compare_constraint(constraint, row, upload, target_set)
        if result['status'] != 'PASS':
            status = 'TARGET_UNMET'
        items.append(TargetEvaluationItem(constraint=constraint, **result))
    evaluation = TargetEvaluation.objects.create(upload=upload, target_set=target_set, context=context, status=status)
    for item in items:
        item.evaluation = evaluation
    TargetEvaluationItem.objects.bulk_create(items)
    return evaluation


def assert_approval_targets(upload, *, expected_context=None):
    """Call inside the approval transaction; always re-evaluate, then CAS the plan.

    The no-op UPDATE obtains the SQLite writer lock for the caller's transaction.
    Approval mutation must occur before that transaction exits.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('目标审批校验必须与批准写入处于同一事务。')
    evaluation = evaluate_upload(upload, expected_context=expected_context)
    context = evaluation.context
    if BudgetPlan.objects.filter(pk=context['plan_id'], revision_token=context['plan_revision']).update(revision_token=F('revision_token')) != 1:
        raise ValidationError('TARGET_EVALUATION_STALE：审批期间年度上下文已变化。')
    fresh = UploadVersion.objects.select_related('cycle__plan', 'template', 'project').get(pk=upload.pk)
    if evaluation_context(fresh) != context:
        raise ValidationError('TARGET_EVALUATION_STALE：审批期间上传上下文已变化。')
    if evaluation.status != 'PASS':
        raise ValidationError('TARGET_UNMET：存在未达标、缺失指标或目标下发前的旧底稿。')
    return evaluation


def compare_constraint(constraint, row, upload, target_set):
    """Read-only presentation and persisted evaluation share the same comparison."""
    actual = delta = shortfall = None
    if upload is None or not upload.template_id:
        status = 'MISSING'
    elif upload.created_at <= target_set.created_at:
        status = 'STALE_UPLOAD'
    elif row is None or row.unit != 'MONEY' or row.data_kind not in ('', 'BUDGET') or row.data_year not in (None, upload.cycle.budget_year):
        status = 'MISSING'
    else:
        actual = _integer(row.value_int) * constraint.sign_multiplier
        delta = actual - constraint.target_int
        if constraint.comparator == 'LE':
            delta = -delta
        elif constraint.comparator == 'EQ':
            delta = -abs(delta)
        shortfall = max(0, -delta)
        status = 'PASS' if delta >= 0 else 'TARGET_UNMET'
    return dict(status=status, actual_int=actual, favorable_delta=delta, shortfall=shortfall)
