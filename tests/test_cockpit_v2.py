import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from budgeting.models import AuditEvent, BudgetCycle, ManagementQuestion, NormalizedValue, Project, ProjectCycle, TemplateVersion, UploadVersion
from budgeting.services.trends import aggregate_metric, build_trend, approved_current_uploads


class CockpitDataTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.project1 = Project.objects.create(code="P001", name="项目一")
        self.project2 = Project.objects.create(code="P002", name="项目二")
        Project.objects.create(code="P003", name="未纳入", is_active=False)
        self.cycle = BudgetCycle.objects.create(name="2026预算", budget_year=2026, status=BudgetCycle.Status.OPEN)
        self.template = TemplateVersion.objects.create(version="V1", budget_year=2026, file_path="x.xlsx", manifest_path="m.json", formula_manifest_hash="0" * 64)
        self.admin = User.objects.create_user(username="admin", password="x", role="ADMIN", is_staff=True)
        self.user1 = User.objects.create_user(username="p001", password="x", role="PROJECT", project=self.project1)
        self.user2 = User.objects.create_user(username="p002", password="x", role="PROJECT", project=self.project2)
        self.uploads = []
        for project in (self.project1, self.project2):
            upload = UploadVersion.objects.create(project=project, cycle=self.cycle, template=self.template, status=UploadVersion.Status.APPROVED, original_path=f"{project.code}.xlsx", sha256=project.code + "0" * 60)
            ProjectCycle.objects.create(project=project, cycle=self.cycle, current_upload=upload)
            self.uploads.append(upload)

    def value(self, upload, row_code, period, value_int=0, unit="MONEY", data_year=None, data_kind="", month=None, ratio_num=None, ratio_den=None, report_code="PL_TOTAL_WINE"):
        return NormalizedValue.objects.create(upload=upload, report_code=report_code, row_code=row_code, row_label=row_code, period=period, data_year=data_year, data_kind=data_kind, month=month, unit=unit, value_int=value_int, ratio_num=ratio_num, ratio_den=ratio_den, source_sheet="损益", source_cell="L1", source_formula="=L1")

    def test_current_upload_and_null_zero_boundary(self):
        self.value(self.uploads[0], "R0032", "B2026", value_int=0, data_year=2026, data_kind="BUDGET")
        self.assertEqual([item.current_upload_id for item in approved_current_uploads(self.cycle)], [item.id for item in self.uploads])
        result = aggregate_metric(self.cycle, "revenue_total", year=2026, kind="BUDGET")
        self.assertEqual(result["value"], 0)
        self.assertEqual(result["included_projects"], 1)
        self.value(self.uploads[0], "R0032", "01", value_int=100, data_year=2026, data_kind="BUDGET", month=1)
        trend = build_trend(self.cycle, "revenue_total")
        budget = next(item for item in trend["series"] if item["year"] == 2026)
        self.assertEqual(budget["values"][0], 100)
        self.assertIsNone(budget["values"][1])

    def test_ratio_is_weighted_by_raw_numerator_denominator(self):
        self.value(self.uploads[0], "R0029", "A2024", unit="RATIO", ratio_num=6000, ratio_den=10000)
        self.value(self.uploads[1], "R0029", "A2024", unit="RATIO", ratio_num=4000, ratio_den=5000)
        result = aggregate_metric(self.cycle, "occ", year=2024, kind="ACTUAL")
        self.assertEqual((result["ratio_num"], result["ratio_den"], result["value"]), (10000, 15000, 6667))

    def test_adr_uses_numerator_and_denominator_and_does_not_average_values(self):
        for upload, revenue, rooms in zip(self.uploads, (10000, 20000), (100, 300)):
            self.value(upload, "R0041", "A2024", value_int=revenue)
            self.value(upload, "R0028", "A2024", unit="COUNT", value_int=rooms)
        result = aggregate_metric(self.cycle, "adr", year=2024, kind="ACTUAL")
        self.assertEqual((result["value"], result["ratio_num"], result["ratio_den"]), (75, 30000, 400))

    def test_derived_metric_without_denominator_is_null_for_company_but_single_project_can_show_annual_raw(self):
        for upload, value in zip(self.uploads, (100, 200)):
            self.value(upload, "R0030", "A2024", value_int=value)
        self.assertIsNone(aggregate_metric(self.cycle, "adr", year=2024, kind="ACTUAL")["value"])
        single = aggregate_metric(self.cycle, "adr", year=2024, kind="ACTUAL", project_id=self.project1.id)
        self.assertEqual(single["value"], 100)

    def test_rolling_series_have_four_kinds_and_annual_only_history_label(self):
        self.value(self.uploads[0], "R0032", "A2023", value_int=10)
        payload = build_trend(self.cycle, "revenue_total")
        self.assertEqual([(item["year"], item["kind"]) for item in payload["series"]], [(2023, "ACTUAL"), (2024, "ACTUAL"), (2025, "FORECAST"), (2026, "BUDGET")])
        item = payload["annual"][0]
        self.assertTrue(item["annual_only"])
        self.assertIn("仅年度", item["label"])
        self.assertEqual(payload["coverage"], {"included": 2, "active": 2})
        self.cycle.budget_year = 2027
        self.cycle.save(update_fields=["budget_year"])
        rolled = build_trend(self.cycle, "revenue_total")
        self.assertEqual([(item["year"], item["kind"]) for item in rolled["series"]], [(2024, "ACTUAL"), (2025, "ACTUAL"), (2026, "FORECAST"), (2027, "BUDGET")])


    def test_room_operating_cost_uses_r9004_over_sold_room_nights(self):
        for upload, amount, sold in zip(self.uploads, (12000, 24000), (300, 600)):
            self.value(upload, "R9004", "A2024", value_int=amount)
            self.value(upload, "R0028", "A2024", unit="COUNT", value_int=sold)

        actual = aggregate_metric(
            self.cycle,
            "cost_room_operating_per_room",
            year=2024,
            kind="ACTUAL",
        )
        self.assertEqual(actual["value"], 40)
        self.assertEqual((actual["ratio_num"], actual["ratio_den"]), (36000, 900))

        self.value(self.uploads[0], "R9004", "A2024", value_int=9000, report_code="PL_ZZ_WINE")
        self.value(self.uploads[0], "R0011", "A2024", unit="COUNT", value_int=180, report_code="PL_ZZ_WINE")
        split = aggregate_metric(
            self.cycle,
            "cost_room_operating_per_room",
            report_code="PL_ZZ_WINE",
            year=2024,
            kind="ACTUAL",
        )
        self.assertEqual(split["value"], 50)

        self.value(self.uploads[0], "R9004", "01", value_int=1000, data_year=2026, data_kind="BUDGET", month=1)
        self.value(self.uploads[0], "R0028", "01", unit="COUNT", value_int=25, data_year=2026, data_kind="BUDGET", month=1)
        trend = build_trend(self.cycle, "cost_room_operating_per_room")
        budget = next(item for item in trend["series"] if item["year"] == 2026)
        self.assertEqual(budget["kind_label"], "预算")
        self.assertEqual(budget["values"][0], 40)

    def test_room_operating_cost_missing_numerator_or_zero_denominator_is_null(self):
        self.value(self.uploads[0], "R0028", "A2024", unit="COUNT", value_int=10)
        self.assertIsNone(aggregate_metric(self.cycle, "cost_room_operating_per_room", year=2024, kind="ACTUAL")["value"])

        self.value(self.uploads[1], "R9004", "A2024", value_int=500)
        self.value(self.uploads[1], "R0028", "A2024", unit="COUNT", value_int=0)
        self.assertIsNone(aggregate_metric(self.cycle, "cost_room_operating_per_room", year=2024, kind="ACTUAL")["value"])

    def test_derived_metric_choice_and_labels_are_available_in_trend_ui(self):
        from pathlib import Path
        from budgeting.services.metrics import METRICS
        from budgeting.services.trends import metric_choices

        choice = next(item for item in metric_choices("PL_TOTAL_WINE") if item["code"] == "cost_room_operating_per_room")
        self.assertTrue(choice["available"])
        self.assertEqual(choice["row_code"], "R9004")
        self.assertEqual(METRICS["profit_npi"]["label"], "NPI（净经营收益）")

        trend = build_trend(self.cycle, "cost_room_operating_per_room")
        self.assertEqual(
            [(item["year"], item["kind_label"]) for item in trend["series"]],
            [(2023, "实际"), (2024, "实际"), (2025, "预测"), (2026, "预算")],
        )
        template = Path("templates/budgeting/cockpit_trend.html").read_text(encoding="utf-8")
        self.assertIn("{{ item.kind_label }}", template)


class CockpitViewTests(CockpitDataTests):
    def test_dashboard_tracks_old_version_questions_and_latest_upload_issues(self):
        from budgeting.models import ValidationIssue, ValidationRun

        old = self.uploads[0]
        ManagementQuestion.objects.create(upload=old, report_code="PL_TOTAL_WINE", row_code="R0032", period="01", body="原版本的问题", created_by=self.admin)
        run = ValidationRun.objects.create(upload=old, rule_version="R1")
        ValidationIssue.objects.create(run=run, severity="P0", code="OLD", message="旧版本问题不计入当前上传异常")
        replacement = UploadVersion.objects.create(project=self.project1, cycle=self.cycle, template=self.template, status="APPROVED", original_path="replacement.xlsx", sha256="a" * 64)
        ProjectCycle.objects.filter(project=self.project1, cycle=self.cycle).update(current_upload=replacement)
        pending = UploadVersion.objects.create(project=self.project2, cycle=self.cycle, template=self.template, status="SUBMITTED", original_path="pending.xlsx", sha256="b" * 64)
        run = ValidationRun.objects.create(upload=pending, rule_version="R1")
        ValidationIssue.objects.create(run=run, severity="P1", code="EXPLAIN", message="待确认")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("cockpit_dashboard"))
        tasks = response.context["management_tasks"]
        self.assertEqual(tasks["submitted_projects"], 1)
        self.assertEqual(tasks["validation_projects"], 1)
        self.assertEqual(tasks["unanswered_questions"], 1)
        self.assertEqual(response.context["question_count"], 1)

    def setUp(self):
        super().setUp()
        self.client = Client()

    def test_admin_dashboard_and_json_export(self):
        self.value(self.uploads[0], "R0041", "YEAR", value_int=10000, data_year=2026, data_kind="BUDGET")
        self.value(self.uploads[0], "R0028", "YEAR", value_int=100, unit="COUNT", data_year=2026, data_kind="BUDGET")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("cockpit_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1.00 元/房晚")
        response = self.client.get(reverse("cockpit_trend_data"), {"metric": "revenue_total"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("series", response.json())
        response = self.client.get(reverse("cockpit_export"), {"metric": "revenue_total"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))

    def test_project_cannot_access_management_or_other_question(self):
        self.client.force_login(self.user1)
        self.assertEqual(self.client.get(reverse("cockpit_dashboard")).status_code, 403)
        self.value(self.uploads[0], "R0032", "01", value_int=1, data_year=2026, data_kind="BUDGET", month=1)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("cockpit_question_new"), {"value_id": NormalizedValue.objects.latest("id").id, "report_code": "PL_TOTAL_WINE", "row_code": "R0032", "period": "01", "body": "请说明该值。"})
        self.assertEqual(response.status_code, 302)
        question = ManagementQuestion.objects.get()
        self.client.force_login(self.user2)
        self.assertEqual(self.client.get(reverse("cockpit_question", kwargs={"question_id": question.id})).status_code, 403)
        self.client.force_login(self.user1)
        self.assertEqual(self.client.get(reverse("cockpit_question", kwargs={"question_id": question.id})).status_code, 200)

    def test_question_snapshot_reply_close_reopen_and_freeze(self):
        value = self.value(self.uploads[0], "R0032", "01", value_int=125, data_year=2026, data_kind="BUDGET", month=1)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("cockpit_question_new"), {"value_id": value.id, "report_code": value.report_code, "row_code": value.row_code, "period": value.period, "body": "请核实。"})
        self.assertEqual(response.status_code, 302)
        question = ManagementQuestion.objects.get()
        snapshot = question.snapshot.copy()
        value.value_int = 999
        value.save(update_fields=["value_int"])
        question.refresh_from_db()
        self.assertEqual(question.snapshot, snapshot)
        self.client.force_login(self.user1)
        response = self.client.post(reverse("cockpit_question", kwargs={"question_id": question.id}), {"action": "reply", "body": "已核实。"})
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.status, "ANSWERED")
        self.client.force_login(self.admin)
        self.client.post(reverse("cockpit_question", kwargs={"question_id": question.id}), {"action": "close"})
        question.refresh_from_db()
        self.assertEqual(question.status, "CLOSED")
        self.client.post(reverse("cockpit_question", kwargs={"question_id": question.id}), {"action": "reopen"})
        question.refresh_from_db()
        self.assertEqual(question.status, "OPEN")
        self.cycle.status = BudgetCycle.Status.FROZEN
        self.cycle.save(update_fields=["status"])
        response = self.client.post(reverse("cockpit_question", kwargs={"question_id": question.id}), {"action": "reply", "body": "冻结后不应写入。"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(question.replies.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="QUESTION_CREATED").exists())


if __name__ == "__main__":
    print(json.dumps({"tests": "django"}))
