"""Keep report dimensions while moving between the existing workspaces."""
from urllib.parse import urlencode
from django import template
from django.urls import reverse
from budgeting.models import BudgetCycle

register = template.Library()


@register.simple_tag(takes_context=True)
def budget_navigation(context):
    request = context.get('request')
    cycle = context.get('cycle')
    plan = context.get('plan')
    if cycle is None and context.get('upload'):
        cycle = context['upload'].cycle
    if cycle is None and request and request.GET.get('cycle', '').isdigit():
        cycle = BudgetCycle.objects.filter(pk=request.GET['cycle']).first()
    if plan and cycle and cycle.plan_id != plan.pk:
        cycle = None
    if cycle is None and plan:
        cycle = BudgetCycle.objects.filter(plan=plan).order_by('-revision_no').first()
    query = {}
    if request:
        for key in ('report','report_code','source_mode','snapshot','period_scope','projects'):
            if request.GET.get(key):
                query[key] = request.GET[key]
    if context.get('source_mode'):
        query['source_mode'] = context['source_mode']
    if cycle:
        query.update(cycle=cycle.pk, year=cycle.budget_year)
    elif plan:
        query['year'] = plan.budget_year
    elif request and request.GET.get('year', '').isdigit():
        query['year'] = int(request.GET['year'])
    report = context.get('report_code')
    if report:
        query.update(report=report, report_code=report)
    plan_id = getattr(cycle, 'plan_id', None) or getattr(plan, 'pk', None)
    return {'query': '?' + urlencode(query) if query else '', 'cycle': cycle,
            'plan_id':plan_id, 'year':query.get('year'),
            'filters': [{'name':key,'value':value} for key,value in query.items() if key not in {'cycle','year'}]}


@register.simple_tag
def annual_target_link(plan_id, project_id, query=''):
    if not plan_id or not project_id:
        return ''
    return reverse('annual_targets', kwargs={'plan_id':plan_id,'project_id':project_id}) + query
