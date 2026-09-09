import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from budgeting.models import (
    BudgetCycle,
    NormalizedValue,
    Project,
    TemplateVersion,
    UploadVersion,
    ValidationRun,
)
from budgeting.services import legacy_rehearsal


def _cell(row, column, value=None, *, formula=None, is_formula=False, number_format="General", error_status=None):
    letters = ""
    number = column
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return {
        "row": row,
        "column": column,
        "coordinate": f"{letters}{row}",
        "cached_value": value,
        "display_value": str(value) if value is not None else None,
        "formula": formula,
        "is_formula": is_formula,
        "number_format": number_format,
        "error_status": error_status,
        "is_error": bool(error_status),
    }


def _sheet(name, rows, *, header_row=20, annual_value=100):
    cells = [_cell(header_row, 1, "利润表科目")]
    for month, column in enumerate(range(2, 14), start=1):
        cells.append(_cell(header_row, column, f"{month:02d}"))
    cells.append(_cell(header_row, 14, "全年合计"))
    cells.append(_cell(header_row, 15, "2026年预算"))
    for row, label, value in rows:
        cells.append(_cell(row, 1, label))
        for column in range(2, 14):
            cells.append(_cell(row, column, value))
        cells.append(_cell(row, 14, annual_value if row != 31 else 999))
        cells.append(_cell(row, 15, 999))
    return {
        "id": name,
        "name": name,
        "state": "visible",
        "visible": True,
        "cells": cells,
    }


def _manifest(report_names):
    reports = {}
    for code, name in report_names.items():
        reports[code] = {
            "sheet": name,
            "mapping": [
                {
                    "row_code": "R0001",
                    "row_label": "收入A",
                    "period": "01",
                    "cell": "B30",
                    "unit": "MONEY",
                    "aggregation": "SUM",
                },
                {
                    "row_code": "R0001",
                    "row_label": "收入A",
                    "period": "FY",
                    "cell": "N30",
                    "unit": "MONEY",
                    "aggregation": "SUM",
                },
                {
                    "row_code": "R0002",
                    "row_label": "收入B",
                    "period": "01",
                    "cell": "B32",
                    "unit": "MONEY",
                    "aggregation": "SUM",
                },
                {
                    "row_code": "R0002",
                    "row_label": "收入B",
                    "period": "FY",
                    "cell": "N32",
                    "unit": "MONEY",
                    "aggregation": "SUM",
                },
            ],
        }
    return {"reports": reports}


class LegacyRehearsalTests(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.manifest_path = self.root / "template-manifest.json"
        self.manifest_path.write_text(
            json.dumps(
                _manifest(
                    {
                        "PL_TOTAL_WINE": "酒店损益总表（含名酒）",
                        "PL_TOTAL_NOWINE": "酒店损益总表（不含名酒）",
                    }
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.template = TemplateVersion.objects.create(
            version="TEST-LEGACY",
            budget_year=2027,
            file_path="template.xlsx",
            manifest_path=str(self.manifest_path),
            formula_manifest_hash="f" * 64,
        )
        self.project = Project.objects.create(code="LEGACY", name="演练项目")
        self.cycle = BudgetCycle.objects.create(
            name="2027演练",
            budget_year=2027,
            source_budget_year=2026,
            status=BudgetCycle.Status.OPEN,
            template=self.template,
        )
        self.source_path = self.root / "original.xlsx"
        self.source_path.write_bytes(b"mock source")
        self.upload = UploadVersion.objects.create(
            project=self.project,
            cycle=self.cycle,
            template=self.template,
            original_name="original.xlsx",
            original_path=str(self.source_path),
            sha256="a" * 64,
        )

    def _run(self, source):
        run = ValidationRun.objects.create(upload=self.upload, rule_version="R3")
        with patch.object(legacy_rehearsal, "read_workbook", return_value=source):
            result = legacy_rehearsal.process_legacy_rehearsal(
                self.upload, self.source_path, run
            )
        self.upload.refresh_from_db()
        run.refresh_from_db()
        return result, run

    def test_inserted_source_row_does_not_shift_canonical_subjects(self):
        source = {
            "source_sha256": self.upload.sha256,
            "safety": {"macros_ignored": False, "external_parts_ignored": False},
            "sheets": [
                _sheet(
                    "酒店损益总表（含名酒）",
                    [(30, "收入A", 10), (31, "中智插入明细", 20), (32, "收入B", 30)],
                )
            ],
        }
        result, _run = self._run(source)
        self.assertTrue(result)
        values = NormalizedValue.objects.filter(upload=self.upload, report_code="PL_TOTAL_WINE")
        self.assertEqual(values.filter(row_code="R0001", period="01").get().value_int, 1000)
        self.assertEqual(values.filter(row_code="R0002", period="01").get().value_int, 3000)
        self.assertTrue(values.filter(row_code="S0031", period="01").exists())

    def test_source_year_offset_preserves_actual_and_forecast_kind(self):
        source = _sheet(
            "酒店损益总表（含名酒）",
            [(30, "收入A", 10), (32, "收入B", 30)],
        )
        source["cells"].extend(
            [
                _cell(20, 16, "2025年实际"),
                _cell(20, 17, "2026年预测"),
                _cell(30, 16, 11),
                _cell(30, 17, 12),
            ]
        )
        result, _run = self._run(source={"sheets": [source]})
        self.assertTrue(result)
        values = NormalizedValue.objects.filter(upload=self.upload, row_code="R0001")
        self.assertTrue(values.filter(period="A2026", data_year=2026, data_kind="ACTUAL").exists())
        self.assertTrue(values.filter(period="F2027", data_year=2027, data_kind="FORECAST").exists())

    def test_missing_cache_is_skipped_instead_of_becoming_zero(self):
        source = _sheet(
            "酒店损益总表（含名酒）",
            [(30, "收入A", None), (32, "收入B", 30)],
        )
        for cell in source["cells"]:
            if cell["row"] == 30 and cell["column"] == 2:
                cell["is_formula"] = True
                cell["formula"] = "=1+1"
        result, run = self._run({"sheets": [source]})
        self.assertTrue(result)
        self.assertFalse(
            NormalizedValue.objects.filter(upload=self.upload, row_code="R0001", period="01").exists()
        )
        self.assertFalse(
            NormalizedValue.objects.filter(upload=self.upload, row_code="R0001", period="01", value_int=0).exists()
        )
        self.assertTrue(run.issues.filter(code="LEGACY_SOURCE_CACHE_GAPS").exists())

    def test_duplicate_year_headings_keep_one_explicit_annual_value(self):
        source = {
            "sheets": [
                _sheet(
                    "酒店损益总表（含名酒）",
                    [(30, "收入A", 10), (32, "收入B", 30)],
                    annual_value=100,
                )
            ]
        }
        result, _run = self._run(source)
        self.assertTrue(result)
        annual = NormalizedValue.objects.filter(
            upload=self.upload, report_code="PL_TOTAL_WINE", row_code="R0001", period="YEAR"
        )
        self.assertEqual(annual.count(), 1)
        self.assertEqual(annual.get().value_int, 10_000)

    def test_missing_report_is_recorded_without_copying_another_report(self):
        source = {
            "sheets": [
                _sheet(
                    "酒店损益总表（含名酒）",
                    [(30, "收入A", 10), (32, "收入B", 30)],
                )
            ]
        }
        result, run = self._run(source)
        self.assertTrue(result)
        self.assertFalse(
            NormalizedValue.objects.filter(upload=self.upload, report_code="PL_TOTAL_NOWINE").exists()
        )
        self.assertTrue(run.issues.filter(code="LEGACY_MISSING_REPORT").exists())

    def test_formal_cycle_without_source_year_is_rejected(self):
        self.cycle.source_budget_year = None
        self.cycle.save(update_fields=["source_budget_year"])
        run = ValidationRun.objects.create(upload=self.upload, rule_version="R3")
        with self.assertRaises(ValueError):
            legacy_rehearsal.process_legacy_rehearsal(self.upload, self.source_path, run)


class CountRoundingTests(TestCase):
    def test_fractional_counts_are_rounded_half_up_with_provenance(self):
        for raw, expected in [(12.4, 12), (12.5, 13), (0.4, 0), (-12.5, -13)]:
            cell = _cell(2, 2, raw)
            cell['_sheet'] = '汇总'
            result = legacy_rehearsal._value(None, 'PL_TOTAL_WINE', 'R0039', '员工人数', 'YEAR', 2027, 'BUDGET', cell, 'COUNT')
            self.assertEqual(result.value_int, expected)
            self.assertEqual(result.source_cell, 'B2')
