from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from budgeting.excel.template_v3 import build_v3
from budgeting.models import TemplateVersion


DEFAULT_SOURCE = settings.SOURCE_WORKBOOK


class Command(BaseCommand):
    help = "按年度构建保留 67 张业务表结构的 V3 本机试填模板。"

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=date.today().year + 1)
        parser.add_argument("--source", default=str(DEFAULT_SOURCE))
        parser.add_argument("--clean-base", default=str(Path(settings.BASE_DIR) / "artifacts" / "平台标准预算模板_V1.xlsx"))
        parser.add_argument("--base-manifest", default=str(Path(settings.BASE_DIR) / "artifacts" / "template_manifest.json"))
        parser.add_argument("--output-dir", default=None)
        parser.add_argument("--register", action="store_true", help="以停用状态登记 TemplateVersion；不会切换现有周期。")

    def handle(self, *args, **options):
        year = options["year"]
        if year < 2000 or year > 2100:
            raise CommandError("--year 必须在 2000—2100 之间。")
        source, clean_base = Path(options["source"]).expanduser(), Path(options["clean_base"]).expanduser()
        if not source.exists():
            raise CommandError(f"原始套表不存在：{source}")
        if not clean_base.exists():
            raise CommandError(f"已净化基础模板不存在：{clean_base}")
        output_dir = Path(options["output_dir"] or (Path(settings.BASE_DIR) / "artifacts" / "v3" / str(year)))
        output = output_dir / f"平台标准预算模板_V3_{year}.xlsx"
        manifest_path = output_dir / f"template_manifest_V3_{year}.json"
        report_path = output_dir / f"模板清理报告_V3_{year}.json"
        manifest, report = build_v3(clean_base, source, options["base_manifest"], output, manifest_path, report_path, year)
        registered = False
        if options["register"]:
            TemplateVersion.objects.update_or_create(
                version=f"V3-{year}",
                defaults={
                    "budget_year": year,
                    "file_path": str(output.relative_to(settings.BASE_DIR)),
                    "manifest_path": str(manifest_path.relative_to(settings.BASE_DIR)),
                    "formula_manifest_hash": manifest["formula_manifest_hash"],
                    "rule_version": manifest["rule_version"],
                    "is_active": False,
                },
            )
            registered = True
        self.stdout.write(json.dumps({"template": str(output), "manifest": str(manifest_path), "report": str(report_path), "status": report["status"], "registered_inactive": registered}, ensure_ascii=False))
