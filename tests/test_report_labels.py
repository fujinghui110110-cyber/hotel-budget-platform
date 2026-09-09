from budgeting.services.report_labels import (
    labels_from_manifest,
    normalize_label,
    report_row_labels,
    resolve_report_labels,
    resolve_row_label,
)
from django.test import SimpleTestCase
from django.test import TestCase
import json
import tempfile
from pathlib import Path
from budgeting.models import BudgetCycle, TemplateVersion


TOTAL_MANIFEST = {
    "reports": {
        "PL_TOTAL_WINE": {
            "mapping": [
                {"row_code": "R0023", "row_label": "房间数", "cell": "J23"},
                {"row_code": "R0024", "row_label": "总可卖房", "cell": "J24"},
                {"row_code": "R0028", "row_label": "已售房", "cell": "J28"},
                {"row_code": "R0105", "row_label": "NPI", "cell": "J105"},
            ]
        },
        "PL_ZZ_WINE": {
            "mapping": [
                {"row_code": "R0023", "row_label": "工资及福利（酒店编）", "cell": "F23"},
                {"row_code": "R0129", "row_label": "NPI", "cell": "F129"},
            ]
        },
    }
}


class ReportLabelsTests(SimpleTestCase):
    def test_manifest_labels_are_scoped_to_report_and_keep_npi_independent(self):
        self.assertEqual(labels_from_manifest(TOTAL_MANIFEST, "PL_TOTAL_WINE")["R0023"], "房间数")
        self.assertEqual(labels_from_manifest(TOTAL_MANIFEST, "PL_TOTAL_WINE")["R0024"], "总可卖房")
        self.assertEqual(labels_from_manifest(TOTAL_MANIFEST, "PL_TOTAL_WINE")["R0028"], "已售房")
        self.assertEqual(resolve_row_label("PL_TOTAL_WINE", "R0105", manifest=TOTAL_MANIFEST), "NPI")
        self.assertEqual(resolve_row_label("PL_ZZ_WINE", "R0129", manifest=TOTAL_MANIFEST), "NPI")
        self.assertEqual(resolve_row_label("PL_ZZ_WINE", "R0105", manifest=TOTAL_MANIFEST), "R0105")


    def test_current_original_label_beats_raw_or_historical_label(self):
        labels = resolve_report_labels(
        "PL_TOTAL_WINE",
        ["R0023", "R0024", "R0028"],
        manifest={
            "reports": {
                "PL_TOTAL_WINE": {
                    "mapping": [
                        {"row_code": "R0023", "row_label": "rawR0023"},
                        {"row_code": "R0024", "row_label": "R0024"},
                        {"row_code": "R0028", "row_label": "R0028"},
                    ]
                }
            }
        },
        current_labels={"R0023": "'  房间数", "R0024": "当前总可卖房"},
        historical_labels={"R0023": "历史名称", "R0024": "历史名称", "R0028": "历史已售房"},
    )
        self.assertEqual(labels, {"R0023": "房间数", "R0024": "当前总可卖房", "R0028": "历史已售房"})


    def test_non_current_records_cannot_override_current_records(self):
        labels = resolve_report_labels(
        "PL_TOTAL_WINE",
        ["R0023"],
        current_labels=[
            {"report_code": "PL_TOTAL_WINE", "row_code": "R0023", "row_label": "当前房间数", "is_current": True},
            {"report_code": "PL_TOTAL_WINE", "row_code": "R0023", "row_label": "旧房间数", "is_current": False},
        ],
    )
        self.assertEqual(labels["R0023"], "当前房间数")


    def test_raw_code_is_fallback_and_values_are_not_an_input_to_label_resolution(self):
        self.assertEqual(normalize_label("  '  总可卖房  "), "总可卖房")
        self.assertEqual(
            resolve_report_labels("PL_TOTAL_WINE", ["R0023"], current_labels={"R0023": "rawR0023"}),
            {"R0023": "R0023"},
        )


    def test_cycle_wrapper_uses_template_manifest_and_limits_result_to_requested_rows(self):
        class Template:
            manifest_path = "not-used.json"

        class Cycle:
            template = Template()

        labels = report_row_labels(
            Cycle(),
            "PL_TOTAL_WINE",
            ["R0023", "R0024", "R0028"],
            {"R0023": "当前房间数"},
            manifest=TOTAL_MANIFEST,
        )
        self.assertEqual(labels, {"R0023": "房间数", "R0024": "总可卖房", "R0028": "已售房"})


    def test_checked_in_v1_manifest_keeps_source_titles_for_total_report(self):
        manifest_path = Path(__file__).resolve().parents[1] / "artifacts" / "template_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        labels = report_row_labels(
            None, "PL_TOTAL_WINE", ["R0023", "R0024", "R0028", "R0105"], manifest=manifest
        )
        self.assertEqual(labels, {"R0023": "房间数", "R0024": "总可卖房", "R0028": "已售房", "R0105": "NPI"})


class ActiveTemplateReportLabelsTests(TestCase):
    def test_cycle_without_template_uses_active_template_manifest(self):
        TemplateVersion.objects.create(
            version="ACTIVE-LABELS",
            budget_year=2026,
            file_path="active.xlsx",
            manifest_path="artifacts/template_manifest.json",
            formula_manifest_hash="0" * 64,
            is_active=True,
        )
        cycle = BudgetCycle.objects.create(name="无显式模板", budget_year=2026)
        labels = report_row_labels(cycle, "PL_TOTAL_WINE", ["R0023", "R0024", "R0028"])
        self.assertEqual(labels, {"R0023": "房间数", "R0024": "总可卖房", "R0028": "已售房"})

    def test_cycle_template_is_kept_over_active_template(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "reports": {
                            "PL_TOTAL_WINE": {
                                "mapping": [
                                    {"row_code": "R0023", "row_label": "显式模板房间数"},
                                ]
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            explicit = TemplateVersion.objects.create(
                version="EXPLICIT-LABELS",
                budget_year=2026,
                file_path="explicit.xlsx",
                manifest_path=str(manifest_path),
                formula_manifest_hash="1" * 64,
                is_active=False,
            )
            TemplateVersion.objects.create(
                version="ACTIVE-LABELS-2",
                budget_year=2026,
                file_path="active.xlsx",
                manifest_path="artifacts/template_manifest.json",
                formula_manifest_hash="2" * 64,
                is_active=True,
            )
            cycle = BudgetCycle.objects.create(name="显式模板", budget_year=2026, template=explicit)
            labels = report_row_labels(cycle, "PL_TOTAL_WINE", ["R0023"])
        self.assertEqual(labels, {"R0023": "显式模板房间数"})
