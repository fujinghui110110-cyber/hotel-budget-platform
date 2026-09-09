from django.conf import settings
from django.core.management.base import BaseCommand

from budgeting.services.workbook_reference import build_reference_cache


class Command(BaseCommand):
    help = "将本机历史套表生成只读浏览缓存，不创建正式预算版本"

    def handle(self, *args, **options):
        root = settings.BASE_DIR / "verification"
        index = build_reference_cache(root / "real-project-inventory" / "sources",
                                      root / "reference-workbooks")
        self.stdout.write(self.style.SUCCESS(
            f"只读来源：{index['stats']['workbook_count']} 个项目，"
            f"{index['stats']['sheet_count']} 张工作表；未创建任何正式版本。"))
