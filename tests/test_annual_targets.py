from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import path, include

urlpatterns = [path("", include("budgeting.target_urls")), path("", include("config.urls"))]
from budgeting.models import BudgetCycle, BudgetPlan, PlanProject, Project, TemplateVersion, UploadVersion, NormalizedValue, User, TargetSet
from budgeting.services.targets import issue_targets, evaluate_upload, evaluation_context, assert_approval_targets


class AnnualTargetTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='target-admin', role='ADMIN')
        self.project = Project.objects.create(code='T1', name='Hotel')
        self.user = User.objects.create_user(username='target-hotel', project=self.project)
        self.plan = BudgetPlan.objects.create(budget_year=2027)
        PlanProject.objects.create(plan=self.plan, project=self.project)
        self.template = TemplateVersion.objects.create(version='target-test', budget_year=2027, file_path='none', manifest_path='none', formula_manifest_hash='a'*64)
        self.cycle = BudgetCycle.objects.create(name='R1', budget_year=2027, template=self.template, plan=self.plan)

    def row(self, kind='REVENUE', value=100000, **extra):
        row = dict(report_code='PL_TOTAL_WINE', row_code='R001', period='YEAR', unit='CNY_CENT', metric_kind=kind, comparator='LE' if kind == 'EXPENSE' else 'GE', target_int=value, sign_multiplier=1, evidence='管理员核对口径', rule_version='confirmed-v1')
        row.update(extra)
        return row

    def issue(self, rows=None, **extra):
        self.plan.refresh_from_db()
        args = dict(actor=self.admin, plan=self.plan, project=self.project, origin_cycle=self.cycle, selected_target_rows=rows if rows is not None else [self.row()], expected_plan_revision=self.plan.revision_token, reason='正式确认目标')
        args.update(extra)
        return issue_targets(**args)

    def upload(self, value=None, cycle=None):
        upload = UploadVersion.objects.create(project=self.project, cycle=cycle or self.cycle, template=self.template, original_path='input.xlsx', sha256='b'*64)
        if value is not None:
            NormalizedValue.objects.create(upload=upload, report_code='PL_TOTAL_WINE', row_code='R001', period='YEAR', unit='MONEY', value_int=value, source_sheet='损益', source_cell='A1', data_year=2027, data_kind='BUDGET')
        return upload

    def test_direction_boundaries_and_negative_profit(self):
        for kind, target, actual, status in [('REVENUE',100000,105000,'PASS'),('EXPENSE',80000,83000,'TARGET_UNMET'),('PROFIT',-10000,-8000,'PASS'),('REVENUE',100000,100000,'PASS'),('EXPENSE',80000,80000,'PASS')]:
            self.issue([self.row(kind, target)])
            self.assertEqual(evaluate_upload(self.upload(actual)).status, status)

    def test_persists_across_rounds_and_excludes_other_year(self):
        self.issue()
        r2 = BudgetCycle.objects.create(name='R2', revision_no=2, budget_year=2027, plan=self.plan, template=self.template)
        r3 = BudgetCycle.objects.create(name='R3', revision_no=3, budget_year=2027, plan=self.plan, template=self.template)
        self.assertEqual(evaluate_upload(self.upload(102000, r2)).status, 'PASS')
        self.assertEqual(evaluate_upload(self.upload(99000, r3)).status, 'TARGET_UNMET')
        other = BudgetPlan.objects.create(budget_year=2028)
        PlanProject.objects.create(plan=other, project=self.project)
        cycle = BudgetCycle.objects.create(name='2028', budget_year=2028, plan=other, template=self.template)
        self.assertEqual(evaluate_upload(self.upload(1, cycle)).items.count(), 0)

    def test_missing_stale_and_no_automatic_derived_constraints(self):
        old = self.upload(120000)
        target = self.issue()
        self.assertEqual(target.constraints.count(), 1)
        self.assertEqual(evaluate_upload(old).items.get().status, 'STALE_UPLOAD')
        self.assertEqual(evaluate_upload(self.upload()).items.get().status, 'MISSING')

    def test_permissions_revision_revoke_and_immutable(self):
        with self.assertRaises(PermissionDenied):
            self.issue(actor=self.user)
        first = self.issue()
        with self.assertRaises(ValidationError):
            self.issue(expected_plan_revision=1)
        second = self.issue([], revoke=True)
        self.assertEqual(second.supersedes, first)
        self.assertEqual(evaluate_upload(self.upload(1)).status, 'PASS')
        with self.assertRaises(ValidationError):
            TargetSet.objects.filter(pk=first.pk).update(reason='erase')

    def test_conflicts_unknown_and_sign(self):
        with self.assertRaises(ValidationError):
            self.issue([self.row('OTHER', 100, comparator='GE'), self.row('OTHER', 90, comparator='LE')])
        with self.assertRaises(ValidationError):
            self.issue([self.row('UNKNOWN')])
        self.issue([self.row('EXPENSE',80000,sign_multiplier=-1)])
        self.assertEqual(evaluate_upload(self.upload(-83000)).status, 'TARGET_UNMET')
        self.assertEqual(evaluate_upload(self.upload(83000)).status, 'PASS')

    def test_approval_reevaluates_and_rejects_stale_context(self):
        self.issue()
        upload = self.upload(105000)
        old_context = evaluation_context(upload)
        self.assertEqual(assert_approval_targets(upload).status, 'PASS')
        self.issue([self.row(value=110000)])
        with self.assertRaises(ValidationError):
            assert_approval_targets(upload, expected_context=old_context)
        with self.assertRaises(ValidationError):
            assert_approval_targets(upload)

    def test_other_project_and_failed_issue_do_not_change_plan(self):
        self.issue()
        other = Project.objects.create(code='T2', name='Other hotel')
        PlanProject.objects.create(plan=self.plan, project=other)
        upload = UploadVersion.objects.create(project=other, cycle=self.cycle, template=self.template, original_path='other.xlsx', sha256='c'*64)
        self.assertEqual(evaluate_upload(upload).items.count(), 0)
        self.plan.refresh_from_db()
        token = self.plan.revision_token
        with self.assertRaises(ValidationError):
            self.issue([self.row(target_int=1.5)])
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.revision_token, token)

    def test_legacy_without_template_only_passes_without_active_targets(self):
        upload = self.upload(105000)
        upload.template = None
        upload.save(update_fields=['template'])
        self.assertEqual(assert_approval_targets(upload).status, 'PASS')
        self.issue()
        fresh = self.upload(105000)
        fresh.template = None
        fresh.save(update_fields=['template'])
        result = evaluate_upload(fresh)
        self.assertEqual(result.items.get().status, 'MISSING')
        with self.assertRaises(ValidationError):
            assert_approval_targets(fresh)

    def test_same_scope_equal_constraint_conflict_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.issue([self.row('OTHER', 100, comparator='GE'), self.row('OTHER', 90, comparator='EQ')])


@override_settings(ROOT_URLCONF=__name__)
class AnnualTargetViewTests(TestCase):
    row = AnnualTargetTests.row
    issue = AnnualTargetTests.issue
    def setUp(self):
        import tempfile
        import json
        from pathlib import Path
        AnnualTargetTests.setUp(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'manifest.json'
        path.write_text(json.dumps({'template_version':self.template.version,'reports':{'PL_TOTAL_WINE':{'mapping':[{'row_code':'R001','row_label':'收入合计','unit':'MONEY','aggregation':'SUM','cell':'A1'}]}}}))
        self.template.manifest_path = str(path)
        self.template.save()
        self.url = f'/plans/{self.plan.pk}/projects/{self.project.pk}/targets/'

    def form(self):
        self.plan.refresh_from_db()
        return dict(revision=self.plan.revision_token,action='save',confirmed='yes',metric='PL_TOTAL_WINE|R001',kind='REVENUE',comparator='GE',amount='1000.00',sign='1',evidence='经核对收入定义',reason='下发收入目标')

    def test_admin_select_and_duplicate_post_is_idempotent(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertContains(response, '收入合计')
        data = self.form()
        self.assertEqual(self.client.post(self.url, data).status_code,302)
        self.assertEqual(self.client.post(self.url, data).status_code,302)
        self.assertEqual(TargetSet.objects.count(),1)
        self.assertEqual(TargetSet.objects.get().constraints.get().target_int,100000)

    def test_project_read_only_and_other_project_denied(self):
        self.issue()
        self.client.force_login(self.user)
        self.assertContains(self.client.get(self.url),'正式目标')
        self.assertEqual(self.client.post(self.url,self.form()).status_code,403)
        other = Project.objects.create(code='T99',name='Other')
        PlanProject.objects.create(plan=self.plan,project=other)
        self.assertEqual(self.client.get(f'/plans/{self.plan.pk}/projects/{other.pk}/targets/').status_code,403)

    def test_invalid_or_unconfirmed_fields_do_not_create_targets(self):
        self.client.force_login(self.admin)
        for extra in ({'confirmed':''},{'kind':'UNKNOWN'},{'amount':'NaN'},{'metric':'PL_FAKE|R001'},{'comparator':'LE'}):
            data = self.form(); data.update(extra)
            self.assertEqual(self.client.post(self.url,data).status_code,200)
            self.assertFalse(TargetSet.objects.exists())
