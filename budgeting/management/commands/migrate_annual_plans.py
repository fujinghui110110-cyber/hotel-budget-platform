"""Inventory by default; writes only to an explicitly acknowledged database copy."""
import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from budgeting.models import BudgetCycle, HistoricalImport, NormalizedValue
from budgeting.services.plan_history import ensure_plan


class Command(BaseCommand):
    help = "Dry-run existing annual plans and history; --apply-copy requires the exact non-default database copy path."

    def add_arguments(self, parser):
        parser.add_argument("--apply-copy", type=Path)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        db = Path(connection.settings_dict["NAME"]).resolve()
        if options["apply_copy"]:
            from django.conf import settings
            copy_path = options["apply_copy"].resolve()
            if copy_path != db or copy_path == (settings.BASE_DIR / "db.sqlite3").resolve():
                raise CommandError("仅允许连接并显式指定隔离数据库副本；禁止默认正式库。")
            if options["dry_run"]:
                raise CommandError("--dry-run 与 --apply-copy 不能同时使用。")
        cycles = list(BudgetCycle.objects.order_by("budget_year", "revision_no").values("id", "budget_year", "revision_no", "plan_id"))
        candidates = list(HistoricalImport.objects.order_by("pk").values("id", "project_id", "data_year", "data_kind", "report_code", "sha256", "active", "confirmed_at"))
        for row in candidates:
            row["id"] = str(row["id"])
            row["migration_state"] = "PENDING_ADMIN_CONFIRMATION"
            row["confirmed_at"] = row["confirmed_at"].isoformat() if row["confirmed_at"] else None
        conflicts = {}
        for row in candidates:
            key = f'{row["project_id"]}:{row["data_year"]}:{row["data_kind"]}:{row["report_code"]}'
            conflicts.setdefault(key, []).append(row["id"])
        result = {"cycles": cycles, "history_candidates": candidates,
                  "possible_conflicts": {k: v for k, v in conflicts.items() if len(v) > 1},
                  "normalized_history_count": NormalizedValue.objects.filter(data_kind__in=["ACTUAL", "FORECAST"]).count(),
                  "history_action": "PENDING_ADMIN_CONFIRMATION; no values promoted or overwritten"}
        result["inventory_hash"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        if options["apply_copy"]:
            with transaction.atomic():
                for cycle in BudgetCycle.objects.order_by("budget_year", "revision_no"):
                    ensure_plan(cycle)
            result["applied"] = "annual plan membership only"
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
