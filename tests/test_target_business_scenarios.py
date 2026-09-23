"""A04/A13/A14/A15: real rule parsing through target and approval services."""
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from budgeting.models import (
    BudgetCycle, NormalizedValue, Project, ProjectCycle, TargetConstraint,
    TemplateVersion, UploadVersion, User,
)
from budgeting.services.plan_history import ensure_plan
from budgeting.services.pnl_graph import parse_formula, _eval
from budgeting.services.targets import issue_targets, evaluate_upload
from budgeting.services.workflow import approve_upload
from budgeting.services.summary_scenarios import (
    create_summary_scenario, calculate_summary_scenario, issue_summary_scenario,
)
from tests import test_summary_scenarios as summary_fixture


class TargetBusinessScenarios(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='acceptance-targets',role='ADMIN')
        self.project = Project.objects.create(code='BUSINESS',name='目标业务验收酒店')
        self.cycle = BudgetCycle.objects.create(name='2027 R1',budget_year=2027,status='OPEN')
        self.pc = ProjectCycle.objects.create(cycle=self.cycle,project=self.project,is_open=True)
        self.plan = ensure_plan(self.cycle)
        self.path = Path(settings.BASE_DIR) / 'artifacts/v3/2027/template_manifest_V3_2027.json'
        raw = self.path.read_bytes()
        self.manifest = json.loads(raw)
        self.template = TemplateVersion.objects.create(version='business-actual-2027',budget_year=2027,
            manifest_path=str(self.path),file_path='unused.xlsx',formula_manifest_hash=hashlib.sha256(raw).hexdigest())
        self.cycle.template = self.template
        self.cycle.save(update_fields=['template'])

    def row(self, code, kind, amount):
        return dict(report_code='PL_ZZ_WINE',row_code=code,period='01',unit='CNY_CENT',
            metric_kind=kind,comparator='LE' if kind=='EXPENSE' else 'GE',target_int=amount,
            sign_multiplier=1,evidence='管理员确认本模板科目定义',rule_version=self.template.formula_manifest_hash)

    def issue(self, rows):
        self.plan.refresh_from_db()
        return issue_targets(actor=self.admin,plan=self.plan,project=self.project,origin_cycle=self.cycle,
            expected_plan_revision=self.plan.revision_token,selected_target_rows=rows,reason='明确选定正式考核指标')

    def candidate(self, values):
        upload = UploadVersion.objects.create(project=self.project,cycle=self.cycle,template=self.template,
            status='SUBMITTED',original_path='acceptance.xlsx',sha256='c'*64)
        for code, amount in values.items():
            NormalizedValue.objects.create(upload=upload,report_code='PL_ZZ_WINE',row_code=code,period='01',
                month=1,data_year=2027,data_kind='BUDGET',unit='MONEY',value_int=amount)
        return upload

    def formula(self, code):
        rows = [row for row in self.manifest['reports']['PL_ZZ_WINE']['mapping']
                if row['row_code']==code and row['period']=='01']
        self.assertEqual(len(rows),1)
        return rows[0]['formula']

    def computed(self, code, inputs):
        # Feed actual template formula into the production parser; values are integer cents.
        def cell(column, row):
            self.assertEqual(column,'F')
            return Decimal(inputs[row])
        def unsupported_range(*args):
            self.fail('Unexpected range in the verified monthly formula')
        value = _eval(parse_formula(self.formula(code)),cell,unsupported_range)
        self.assertEqual(Decimal(value),Decimal(value).to_integral_value())
        return int(value)

    def test_a04_income_profit_pass_cannot_hide_expense_failure(self):
        self.issue([self.row('R0032','REVENUE',100000),self.row('R0103','PROFIT',20000),
                    self.row('R0099','EXPENSE',80000)])
        candidate = self.candidate({'R0032':105000,'R0103':22000,'R0099':83000})
        result = evaluate_upload(candidate)
        statuses = {item.constraint.metric_kind:item.status for item in result.items.select_related('constraint')}
        self.assertEqual(statuses,{'REVENUE':'PASS','PROFIT':'PASS','EXPENSE':'TARGET_UNMET'})
        self.assertEqual(result.status,'TARGET_UNMET')
        with self.assertRaisesMessage(ValueError,'TARGET_UNMET'):
            approve_upload(candidate,self.admin)
        candidate.refresh_from_db(); self.pc.refresh_from_db()
        self.assertEqual(candidate.status,'SUBMITTED')
        self.assertIsNone(self.pc.current_upload_id)

    def test_a13_derived_profit_target_does_not_overwrite_formula_result(self):
        self.assertEqual(self.formula('R0103'),'ROUND(F101-F102,2)')
        actual = self.computed('R0103',{101:100000,102:15000})
        before = self.candidate({'R0101':100000,'R0102':15000,'R0103':actual})
        target_set = self.issue([self.row('R0103','PROFIT',90000)])
        self.assertEqual(target_set.constraints.get().target_int,90000)
        self.assertEqual(NormalizedValue.objects.get(upload=before,row_code='R0103').value_int,85000)
        fresh = self.candidate({'R0101':100000,'R0102':15000,'R0103':self.computed('R0103',{101:100000,102:15000})})
        with self.assertRaisesMessage(ValueError,'TARGET_UNMET'):
            approve_upload(fresh,self.admin)
        adjusted_profit = self.computed('R0103',{101:105000,102:15000})
        updated = self.candidate({'R0101':105000,'R0102':15000,'R0103':adjusted_profit})
        approve_upload(updated,self.admin)
        self.assertEqual(NormalizedValue.objects.get(upload=updated,row_code='R0103').value_int,90000)
        self.assertEqual(self.formula('R0103'),'ROUND(F101-F102,2)')

    def test_a15_template_four_percent_fee_increase_without_selected_cap_passes(self):
        self.assertEqual(self.formula('R0105'),'+F103*0.04')
        before_fee = self.computed('R0105',{103:100000})
        after_fee = self.computed('R0105',{103:120000})
        self.assertEqual((before_fee,after_fee),(4000,4800))
        target_set = self.issue([self.row('R0103','PROFIT',110000)])
        candidate = self.candidate({'R0103':120000,'R0105':after_fee})
        self.assertFalse(target_set.constraints.filter(row_code='R0105').exists())
        self.assertEqual(evaluate_upload(candidate).status,'PASS')
        approve_upload(candidate,self.admin)
        self.pc.refresh_from_db()
        self.assertEqual(self.pc.current_upload_id,candidate.pk)
        self.assertEqual(NormalizedValue.objects.get(upload=candidate,row_code='R0105').value_int,4800)


class ExplicitSummarySelectionScenarios(TestCase):
    # Reuse the existing full four-report fixture, not its test methods.
    _seed_values = summary_fixture.SummaryScenarioTests._seed_values

    def setUp(self):
        summary_fixture.SummaryScenarioTests.setUp(self)
        self.plan = ensure_plan(self.cycle)

    def test_a14_changed_rows_remain_impacts_without_explicit_target_selection(self):
        scenario = create_summary_scenario(self.upload,'收入调整影响分析',self.admin,'PL_TOTAL_WINE')
        calculate_summary_scenario(scenario,{'R0041':'16800.00','R0042':'130.00'},self.admin)
        scenario.refresh_from_db()
        changes = scenario.results['changed_rows']
        self.assertTrue(any(row['entered'] for row in changes))
        self.assertTrue(any(not row['entered'] for row in changes))
        batch = issue_summary_scenario(scenario,self.admin)
        self.assertTrue(batch.cascade['informational_only'])
        self.assertEqual(batch.cascade['requirements_source'],'annual_targets')
        self.assertEqual(len(batch.cascade['changes']),len(changes))
        self.assertFalse(TargetConstraint.objects.filter(target_set__plan=self.plan).exists())
        self.assertEqual(evaluate_upload(self.upload).items.count(),0)
        candidate = UploadVersion.objects.create(project=self.project,cycle=self.cycle,template=self.template,
            status='SUBMITTED',original_path='unchanged-budget.xlsx',sha256='d'*64)
        for value in NormalizedValue.objects.filter(upload=self.upload,data_kind='BUDGET'):
            NormalizedValue.objects.create(upload=candidate,report_code=value.report_code,row_code=value.row_code,
                period=value.period,data_year=value.data_year,data_kind=value.data_kind,unit=value.unit,
                value_int=value.value_int,ratio_num=value.ratio_num,ratio_den=value.ratio_den)
        # The unselected scenario figures must not become a hidden approval gate.
        approve_upload(candidate,self.admin)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status,'APPROVED')
