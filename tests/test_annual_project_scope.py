from django.test import TestCase
from budgeting.models import BudgetPlan, PlanProject, BudgetCycle, Project, ProjectCycle, TemplateVersion, UploadVersion, NormalizedValue
from budgeting.services.workflow import _company_value_details, project_value_details, company_trend, project_trend_rows
from budgeting.services.trends import approved_current_uploads, latest_report_uploads
from budgeting.services.version_reports import usable_uploads


class AnnualProjectScopeTests(TestCase):
    def setUp(self):
        self.plan=BudgetPlan.objects.create(budget_year=2027)
        self.template=TemplateVersion.objects.create(version='V1',budget_year=2027)
        self.cycles=[BudgetCycle.objects.create(name=f'R{r}',budget_year=2027,revision_no=r,plan=self.plan,template=self.template) for r in (1,2)]
        self.projects=[Project.objects.create(code=f'P{i}',name=f'酒店{i}',is_active=i==0) for i in (0,1)]
        for project in self.projects:
            PlanProject.objects.create(plan=self.plan,project=project)
        for cycle,amounts in zip(self.cycles,[(10000,20000),(70000,80000)]):
            for project,amount in zip(self.projects,amounts):
                upload=UploadVersion.objects.create(cycle=cycle,project=project,template=self.template,status='APPROVED')
                ProjectCycle.objects.create(cycle=cycle,project=project,current_upload=upload)
                NormalizedValue.objects.create(upload=upload,report_code='PL_TOTAL_WINE',row_code='ROOM',period='01',data_year=2027,data_kind='BUDGET',month=1,unit='MONEY',value_int=amount)

    def test_inactive_member_remains_in_company_and_project_trends(self):
        cycle=self.cycles[0]
        self.assertEqual(_company_value_details(cycle,'PL_TOTAL_WINE')[('ROOM','01')]['value_int'],30000)
        self.assertEqual(company_trend(cycle,'PL_TOTAL_WINE','ROOM')['01']['value_int'],30000)
        self.assertEqual(len(project_trend_rows(cycle,'PL_TOTAL_WINE','ROOM')),2)
        self.assertEqual(project_value_details(self.projects[1],cycle,'PL_TOTAL_WINE')[('ROOM','01')]['value_int'],20000)
        self.assertEqual(len(approved_current_uploads(cycle)),2)
        self.assertEqual(len(latest_report_uploads(cycle)),2)
        projects,uploads=usable_uploads(cycle)
        self.assertEqual(projects.count(),2)
        self.assertEqual(len(uploads),2)

    def test_revisions_are_not_mixed(self):
        totals=[company_trend(c,'PL_TOTAL_WINE','ROOM')['01']['value_int'] for c in self.cycles]
        self.assertEqual(totals,[30000,150000])
        outside=Project.objects.create(code='OUT',name='年度外酒店')
        upload=UploadVersion.objects.create(cycle=self.cycles[0],project=outside,template=self.template,status='APPROVED')
        ProjectCycle.objects.create(cycle=self.cycles[0],project=outside,current_upload=upload)
        self.assertEqual(len(approved_current_uploads(self.cycles[0])),2)

    def test_adjustment_entry_points_keep_inactive_annual_member(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        from budgeting.services.workflow import _project_upload
        from budgeting.summary_views import _baselines
        cycle = self.cycles[0]
        project = self.projects[1]
        upload = _project_upload(project, cycle)
        self.assertEqual(upload.project_id, project.pk)
        self.assertEqual(upload.cycle_id, cycle.pk)
        self.assertIn(project.pk, _baselines(cycle))
        outside = Project.objects.create(code='OUTSIDE', name='计划外项目')
        with self.assertRaisesMessage(ValueError, '年度计划范围'):
            _project_upload(outside, cycle)
        admin = get_user_model().objects.create_user(username='scope-admin', role='ADMIN')
        self.client.force_login(admin)
        for route in ('management_adjustments', 'summary_list'):
            response = self.client.get(reverse(route), {'cycle': cycle.pk})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, project.name)
            self.assertNotContains(response, outside.name)
