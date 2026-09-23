from django.test import TestCase, RequestFactory
from django.template.loader import render_to_string
from django.contrib.auth import get_user_model
from django.urls import reverse
from budgeting.models import BudgetPlan, BudgetCycle, Project
from budgeting.templatetags.budget_navigation import budget_navigation, annual_target_link


class WorkspaceNavigationTests(TestCase):
    def setUp(self):
        self.plan=BudgetPlan.objects.create(budget_year=2027)
        self.cycle=BudgetCycle.objects.create(name='2027 R2',budget_year=2027,revision_no=2,plan=self.plan)
        self.project=Project.objects.create(code='P1',name='酒店一')

    def page(self, role='ADMIN', cycle=None):
        request=RequestFactory().get('/',{'cycle':self.cycle.pk,'report':'PL_ZZ_NOWINE','source_mode':'APPROVED'})
        request.user=get_user_model()(username='test',role=role,project=self.project if role=='PROJECT' else None)
        return render_to_string('base.html',{'cycle':cycle or self.cycle,'cycles':[cycle or self.cycle]},request=request)

    def test_four_workspaces_and_auxiliary_routes_remain(self):
        html=self.page()
        for label in ['年度预算总览','酒店预算','汇总调整与下发','历史基准与归档','数据审计','批量上传预算','批量底稿导出','系统更新','公网访问']:
            self.assertIn(label,html)
        self.assertIn('source_mode=APPROVED',html)
        self.assertIn('report=PL_ZZ_NOWINE',html)
        self.assertIn(f'cycle={self.cycle.pk}',html)
        self.assertIn('name="source_mode" value="APPROVED"',html)

    def test_project_only_has_own_target_link(self):
        html=self.page('PROJECT')
        self.assertIn(reverse('annual_targets',kwargs={'plan_id':self.plan.pk,'project_id':self.project.pk}),html)
        self.assertIn('上级要求与达标情况',html)
        self.assertNotIn('批量上传预算',html)

    def test_unbound_plan_does_not_reverse_invalid_url(self):
        old=BudgetCycle.objects.create(name='旧版',budget_year=2026)
        html=self.page('PROJECT',old)
        self.assertNotIn('上级要求与达标情况',html)
        self.assertEqual(annual_target_link(None,self.project.pk),'')

    def test_plan_page_uses_own_plan_cycle(self):
        other=BudgetPlan.objects.create(budget_year=2028)
        request=RequestFactory().get('/',{'cycle':self.cycle.pk})
        nav=budget_navigation({'request':request,'plan':other})
        self.assertEqual(nav['plan_id'],other.pk)
        self.assertIsNone(nav['cycle'])
