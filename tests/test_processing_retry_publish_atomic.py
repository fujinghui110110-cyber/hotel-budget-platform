import shutil
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings

from budgeting.excel.ooxml import sha256_file
from budgeting.models import (
    AuditEvent,
    BudgetCycle,
    NormalizedValue,
    ProcessingJob,
    ProcessingRun,
    Project,
    TemplateVersion,
    UploadVersion,
    ValidationIssue,
)
from budgeting.services.processing_runs import enqueue_processing_job
from budgeting.services.processing_runs import ensure_job_run
from budgeting.services.workflow import freeze_preconditions, process_upload, process_upload_now, save_upload, submit_upload


class ProcessingRetryPublishAtomicTests(TestCase):
    def test_inline_result_refreshes_status_for_upload_page_message(self):
        from budgeting.services.workflow import process_upload_now
        upload = self._upload()
        enqueue_processing_job(upload)
        def reject_in_database(instance):
            UploadVersion.objects.filter(pk=instance.pk).update(status=UploadVersion.Status.REJECTED)
            return False
        with patch('budgeting.services.workflow.process_upload', side_effect=reject_in_database):
            process_upload_now(upload)
        self.assertEqual(upload.status, UploadVersion.Status.REJECTED)

    def test_inline_retry_claims_new_job_instead_of_completed_original(self):
        from budgeting.services.workflow import _claim_inline_upload_job
        upload = self._upload()
        old = ProcessingJob.objects.create(upload=upload, idempotency_key='old', status=ProcessingJob.Status.DONE)
        upload.status = UploadVersion.Status.REJECTED
        upload.save()
        new, _ = enqueue_processing_job(upload, reason='retry')
        claimed, did_claim = _claim_inline_upload_job(upload)
        self.assertTrue(did_claim)
        self.assertEqual(claimed.pk, new.pk)
        old.refresh_from_db()
        self.assertEqual(old.status, ProcessingJob.Status.DONE)

    def test_mapping_guard_accepts_save_only_flags_but_rejects_changed_formula(self):
        from budgeting.models import ValidationRun
        from budgeting.services.workflow import _record_formula_mapping_issues
        import json
        self.manifest_path.write_text(json.dumps({'reports': {'PL_TOTAL_WINE': {'sheet': '汇总', 'mapping': [{'cell': 'A1'}]}}}))
        upload = self._upload()
        run = ValidationRun.objects.create(upload=upload, rule_version='test')
        expected = dict(sheet='汇总', cell='A1', formula='01', shared_attributes={})
        actual = dict(expected, formula='1', shared_attributes={'ca': '1'})
        with patch('budgeting.services.workflow.formula_manifest', side_effect=[([actual], ''), ([expected], '')]):
            _record_formula_mapping_issues(run, upload, self.template_path)
        self.assertFalse(run.issues.exists())
        actual['formula'] = '2'
        with patch('budgeting.services.workflow.formula_manifest', side_effect=[([actual], ''), ([expected], '')]):
            _record_formula_mapping_issues(run, upload, self.template_path)
        self.assertTrue(run.issues.filter(code='FORMULA_CHANGED').exists())

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.storage = self.root / "storage"
        self.storage.mkdir()
        self.override = override_settings(BUDGET_STORAGE_ROOT=self.storage, SOFFICE_BIN="soffice")
        self.override.enable()
        self.project = Project.objects.create(code="P001", name="项目一")
        self.cycle = BudgetCycle.objects.create(name="2027预算", budget_year=2027, status=BudgetCycle.OPEN)
        self.template_path = self.root / "template.xlsx"
        self.template_path.write_bytes(b"template")
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text('{"reports": {}}', encoding="utf-8")
        self.template = TemplateVersion.objects.create(
            version="V1",
            budget_year=2027,
            file_path=str(self.template_path),
            manifest_path=str(self.manifest_path),
            formula_manifest_hash="0" * 64,
            rule_version="R1",
        )

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.root, ignore_errors=True)

    def _upload(self, name="source.xlsx", content=b"workbook"):
        rel = Path("uploads") / self.project.code / name
        path = self.storage / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            template=self.template,
            status=UploadVersion.RECEIVED,
            original_name=name,
            original_path=str(rel),
            sha256=sha256_file(path),
        )

    def _success_patches(self):
        def write_value(upload, workbook_path, validation_run=None):
            NormalizedValue.objects.create(
                upload=upload,
                report_code="PL_TOTAL_WINE",
                row_code="R0001",
                row_label="收入",
                period="YEAR",
                data_year=2027,
                data_kind="BUDGET",
                unit=NormalizedValue.Unit.MONEY,
                value_int=100,
                source_sheet="汇总",
                source_cell="A1",
            )
            return 1

        return [
            patch("budgeting.services.workflow.validate_xlsx_zip", return_value=[]),
            patch("budgeting.services.workflow.validate_upload_contract", return_value=[]),
            patch("budgeting.services.workflow.validate_legacy_balancing_inputs"),
            patch("budgeting.services.workflow._record_formula_mapping_issues"),
            patch("budgeting.services.workflow.recalc_with_libreoffice", side_effect=lambda source, _bin: source),
            patch("budgeting.services.workflow.cached_errors", return_value=[]),
            patch("budgeting.services.workflow.extract_report_values", side_effect=write_value),
            patch("budgeting.services.workflow.extract_sub_table_values"),
            patch("budgeting.services.workflow.extract_management_values"),
            patch("budgeting.services.workflow.validate_management_values"),
            patch("budgeting.services.historical_data.sync_history"),
        ]

    def test_retry_success_uses_current_run_and_ignores_old_p0(self):
        upload = self._upload()
        with patch(
            "budgeting.services.workflow.validate_xlsx_zip",
            return_value=[("P0", "BROKEN_ZIP", "zip rejected", "", "", "")],
        ):
            self.assertFalse(process_upload(upload))
        failed_run = ProcessingRun.objects.get(upload=upload)
        self.assertEqual(failed_run.status, ProcessingRun.Status.FAILED)
        self.assertTrue(ValidationIssue.objects.filter(run__processing_run=failed_run, severity="P0").exists())

        patches = self._success_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
            self.assertTrue(process_upload(upload))

        upload.refresh_from_db()
        self.assertEqual(upload.status, UploadVersion.Status.VALIDATED)
        self.assertEqual(upload.processing_current_run.status, ProcessingRun.Status.SUCCEEDED)
        self.assertNotEqual(upload.processing_current_run_id, failed_run.pk)
        self.assertEqual(NormalizedValue.objects.filter(upload=upload, processing_run=upload.processing_current_run).count(), 1)

        submit_upload(upload)
        self.assertNotIn("存在 P0 阻断问题", freeze_preconditions(self.cycle))

    def test_extraction_exception_rolls_back_draft_values(self):
        upload = self._upload()

        def write_then_fail(upload, workbook_path, validation_run=None):
            NormalizedValue.objects.create(
                upload=upload,
                report_code="PL_TOTAL_WINE",
                row_code="R0001",
                period="YEAR",
                unit=NormalizedValue.Unit.MONEY,
                value_int=100,
                source_sheet="汇总",
                source_cell="A1",
            )
            raise RuntimeError("extract failed")

        patches = self._success_patches()
        patches[6] = patch("budgeting.services.workflow.extract_report_values", side_effect=write_then_fail)
        ProcessingJob.objects.create(upload=upload, idempotency_key="upload:test")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
            job = process_upload_now(upload)

        upload.refresh_from_db()
        self.assertEqual(job.status, ProcessingJob.Status.FAILED)
        self.assertEqual(upload.status, UploadVersion.Status.REJECTED)
        self.assertFalse(NormalizedValue.objects.filter(upload=upload).exists())

    def test_duplicate_failed_upload_reuses_original_and_queues_new_run(self):
        original = save_upload(self.project, self.cycle, SimpleUploadedFile("budget.xlsx", b"same bytes"))
        original.status = UploadVersion.Status.REJECTED
        original.save(update_fields=["status"])
        ProcessingJob.objects.filter(upload=original).update(status=ProcessingJob.Status.FAILED)
        old_job_count = ProcessingJob.objects.filter(upload=original).count()

        duplicate = save_upload(self.project, self.cycle, SimpleUploadedFile("budget.xlsx", b"same bytes"))

        self.assertEqual(duplicate.pk, original.pk)
        self.assertEqual(UploadVersion.objects.filter(project=self.project, cycle=self.cycle, sha256=original.sha256).count(), 1)
        self.assertEqual(ProcessingJob.objects.filter(upload=original).count(), old_job_count + 1)
        self.assertTrue(AuditEvent.objects.filter(action="PROCESSING_JOB_QUEUED", upload=original).exists())

    def test_frozen_approved_upload_cannot_be_requeued(self):
        upload = self._upload()
        upload.status = UploadVersion.Status.APPROVED
        upload.save(update_fields=["status"])
        self.cycle.status = BudgetCycle.Status.FROZEN
        self.cycle.save(update_fields=["status"])

        with self.assertRaisesMessage(ValueError, "冻结周期不允许重排"):
            enqueue_processing_job(upload, reason="manual_retry")

    def test_validated_upload_cannot_requeue_same_upload(self):
        upload = self._upload()
        upload.status = UploadVersion.Status.VALIDATED
        upload.save(update_fields=["status"])

        with self.assertRaisesMessage(ValueError, "不能重排同一上传"):
            enqueue_processing_job(upload, reason="manual_retry")

    def test_retry_requires_admin_or_matching_project_role(self):
        upload = self._upload()
        upload.status = UploadVersion.Status.REJECTED
        upload.save(update_fields=["status"])
        ProcessingJob.objects.create(upload=upload, idempotency_key="upload:old", status=ProcessingJob.Status.FAILED)
        other_project = Project.objects.create(code="P002", name="项目二")
        user_model = get_user_model()
        same_project = user_model.objects.create_user(username="same", role=user_model.Role.PROJECT, project=self.project)
        other_project_user = user_model.objects.create_user(username="other", role=user_model.Role.PROJECT, project=other_project)

        job, created = enqueue_processing_job(upload, actor=same_project, reason="project_retry")
        self.assertTrue(created)
        self.assertEqual(job.upload_id, upload.pk)

        upload_two = self._upload("source-2.xlsx", b"workbook-2")
        upload_two.status = UploadVersion.Status.REJECTED
        upload_two.save(update_fields=["status"])
        ProcessingJob.objects.create(upload=upload_two, idempotency_key="upload:old-2", status=ProcessingJob.Status.FAILED)
        with self.assertRaisesMessage(ValueError, "无权重排"):
            enqueue_processing_job(upload_two, actor=other_project_user, reason="project_retry")


class ProcessingRunConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.project = Project.objects.create(code="P001", name="项目一")
        self.cycle = BudgetCycle.objects.create(name="2027预算", budget_year=2027, status=BudgetCycle.OPEN)
        self.template = TemplateVersion.objects.create(
            version="V1",
            budget_year=2027,
            file_path="template.xlsx",
            manifest_path="manifest.json",
            formula_manifest_hash="0" * 64,
            rule_version="R1",
        )

    def _upload(self, status=UploadVersion.REJECTED):
        return UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            template=self.template,
            status=status,
            original_path="uploads/P001/source.xlsx",
            sha256="0" * 64,
        )

    def test_concurrent_retry_creates_one_active_job(self):
        upload = self._upload()
        ProcessingJob.objects.create(upload=upload, idempotency_key="upload:old", status=ProcessingJob.Status.FAILED)
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def enqueue():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                job, created = enqueue_processing_job(upload, reason="threaded_retry")
                results.append((job.pk, created))
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=enqueue), threading.Thread(target=enqueue)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertFalse(errors, [repr(error) for error in errors])
        self.assertEqual(len(results), 2)
        self.assertEqual(
            ProcessingJob.objects.filter(upload=upload, status__in=[ProcessingJob.Status.QUEUED, ProcessingJob.Status.RUNNING]).count(),
            1,
        )
        self.assertEqual(ProcessingRun.objects.filter(upload=upload).count(), 1)
        self.assertEqual(sorted(created for _, created in results), [False, True])

    def test_concurrent_ensure_job_run_creates_one_run(self):
        upload = self._upload(status=UploadVersion.RECEIVED)
        job = ProcessingJob.objects.create(upload=upload, idempotency_key="upload:pending")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def ensure():
            close_old_connections()
            try:
                local_job = ProcessingJob.objects.get(pk=job.pk)
                barrier.wait(timeout=10)
                run = ensure_job_run(local_job)
                results.append(run.pk)
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=ensure), threading.Thread(target=ensure)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertFalse(errors, [repr(error) for error in errors])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(ProcessingRun.objects.filter(upload=upload).count(), 1)
