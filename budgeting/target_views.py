"""Explicit annual target selection, separate from spreadsheet formula changes."""
from decimal import Decimal, InvalidOperation
import hashlib
import json
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from budgeting.models import BudgetPlan, BudgetCycle, Project, PlanProject, UploadVersion, NormalizedValue, REPORTS, TargetSet
from budgeting.services.metric_definitions import load_definitions
from budgeting.services.targets import issue_targets, latest_targets, compare_constraint


def _amount(value):
    return '—' if value is None else f'{Decimal(value) / 100:,.2f}'


@login_required
def targets(request, plan_id, project_id):
    plan = get_object_or_404(BudgetPlan, pk=plan_id)
    project = get_object_or_404(Project, pk=project_id)
    get_object_or_404(PlanProject, plan=plan, project=project)
    admin = request.user.is_admin_role
    if not admin and request.user.project_id != project.pk:
        return HttpResponseForbidden('无权查看其他项目目标。')
    cycle = BudgetCycle.objects.filter(plan=plan).order_by('-revision_no').first()
    current = latest_targets(plan, project)
    definitions, catalog_error = {}, ''
    if cycle:
        try:
            for report_code in REPORTS:
                rows, version = load_definitions(cycle.template, report_code)
                for row in rows.values():
                    if row.unit == 'MONEY':
                        definitions[f'{report_code}|{row.row_code}'] = (row, version)
        except (OSError, ValueError, KeyError) as exc:
            definitions = {}
            catalog_error = f'指标字典不可用，暂不能新增目标：{exc}'
    if request.method == 'POST':
        if not admin:
            return HttpResponseForbidden('只有管理员可以修订或撤销目标。')
        try:
            action = request.POST.get('action')
            payload = {k: request.POST.get(k) for k in request.POST if k != 'csrfmiddlewaretoken'}
            request_key = hashlib.sha256(json.dumps([plan.pk, project.pk, payload], sort_keys=True).encode()).hexdigest()
            if TargetSet.objects.filter(plan=plan, project=project, source__request_key=request_key).exists():
                messages.info(request, '该操作已完成，无需重复下发。')
                return redirect('annual_targets', plan_id=plan.pk, project_id=project.pk)
            constraints = list(current.constraints.values('report_code','row_code','period','unit','metric_kind','comparator','target_int','sign_multiplier','evidence','rule_version')) if current else []
            if action == 'revoke':
                constraints = []
            elif action == 'save':
                if request.POST.get('confirmed') != 'yes':
                    raise ValidationError('请明确确认选定指标、金额单位与符号。')
                selected = request.POST.get('metric', '')
                if selected not in definitions:
                    raise ValidationError('请选择当前模板四套汇总表中的金额指标。')
                definition, version = definitions[selected]
                report_code, row_code = selected.split('|', 1)
                amount = Decimal(request.POST.get('amount', ''))
                if not amount.is_finite() or amount * 100 != (amount * 100).to_integral_value():
                    raise ValidationError('金额须为有限数字且最多两位小数。')
                record = dict(report_code=report_code,row_code=row_code,period='YEAR',unit='CNY_CENT',metric_kind=request.POST.get('kind'), comparator=request.POST.get('comparator'),target_int=int(amount*100),sign_multiplier=int(request.POST.get('sign','0')),evidence=request.POST.get('evidence','').strip(),rule_version=version)
                constraints = [r for r in constraints if (r['report_code'],r['row_code'],r['period']) != (report_code,row_code,'YEAR')]
                constraints.append(record)
            else:
                raise ValidationError('未知操作。')
            issue_targets(actor=request.user,plan=plan,project=project,origin_cycle=cycle,selected_target_rows=constraints,expected_plan_revision=int(request.POST.get('revision','0')),reason=request.POST.get('reason',''),source={'request_key':request_key,'surface':'annual_targets'},revoke=action=='revoke')
            messages.success(request, '目标已撤销并保留记录。' if action=='revoke' else '选定目标已下发，其他现行目标保持生效。')
            return redirect('annual_targets',plan_id=plan.pk,project_id=project.pk)
        except (ValidationError,ValueError,InvalidOperation) as exc:
            messages.error(request, '；'.join(exc.messages) if isinstance(exc,ValidationError) else str(exc))
    upload = UploadVersion.objects.filter(cycle__plan=plan,project=project,status__in=['VALIDATED','SUBMITTED','APPROVED']).select_related('cycle').order_by('-created_at').first()
    values = NormalizedValue.objects.filter(upload=upload) if upload else NormalizedValue.objects.none()
    if upload and upload.processing_current_run_id:
        values = values.filter(processing_run_id=upload.processing_current_run_id)
    values = {(v.report_code,v.row_code,v.period):v for v in values}
    statuses = {'PASS':'已达标','TARGET_UNMET':'未达标','MISSING':'缺少数据','STALE_UPLOAD':'目标下发前底稿，待重新上传'}
    rows = []
    for constraint in current.constraints.all() if current else []:
        value = values.get((constraint.report_code,constraint.row_code,constraint.period))
        result = compare_constraint(constraint,value,upload,current)
        definition = definitions.get(f'{constraint.report_code}|{constraint.row_code}')
        rows.append(dict(label=definition[0].label if definition else (value.row_label if value else constraint.row_code),report=REPORTS.get(constraint.report_code,constraint.report_code),comparison={'GE':'不低于','LE':'不高于','EQ':'等于'}[constraint.comparator],target=_amount(constraint.target_int),actual=_amount(result['actual_int']),gap=_amount(result['shortfall']),status=statuses[result['status']],evidence=constraint.evidence))
    options = [dict(key=key,label=f'{REPORTS[key.split("|")[0]]} · {definition.label}') for key,(definition,_) in definitions.items()]
    plan.refresh_from_db()
    return render(request,'budgeting/targets.html',dict(plan=plan,project=project,is_admin=admin,current=current,rows=rows,options=options,catalog_error=catalog_error,upload=upload))
