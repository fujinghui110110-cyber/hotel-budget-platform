from django.conf import settings
from django.core.management.base import BaseCommand

from budgeting.excel.processor import sanitize_template
from budgeting.models import TemplateVersion
from budgeting.services.files import rel, sha256_file


class Command(BaseCommand):
    help = "Create platform template V1 and manifest from the read-only source workbook."

    def handle(self, *args, **options):
        source = settings.SOURCE_WORKBOOK
        if sha256_file(source) != settings.SOURCE_SHA256:
            raise SystemExit("源套表 SHA-256 与实施方案不一致，停止。")
        out_file = settings.BASE_DIR / "artifacts" / "平台标准预算模板_V1.xlsx"
        manifest_file = settings.BASE_DIR / "artifacts" / "template_manifest_V1.json"
        manifest = sanitize_template(source, out_file, manifest_file)
        TemplateVersion.objects.update_or_create(
            version="V1",
            defaults={
                "budget_year": 2026,
                "file_path": rel(out_file),
                "manifest_path": rel(manifest_file),
                "formula_manifest_hash": manifest["formula_manifest_hash"],
                "rule_version": "R1",
                "is_active": True,
            },
        )
        self.stdout.write(self.style.SUCCESS(f"created {out_file}"))
