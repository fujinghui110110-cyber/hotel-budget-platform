from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from budgeting.models import BudgetCycle, Project


class Command(BaseCommand):
    help = "Create local demo admin/project users and one open cycle."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("正式环境禁止创建演示账号，请使用 createsuperuser 和组织账号管理。")
        User = get_user_model()
        cycle, _ = BudgetCycle.objects.get_or_create(
            budget_year=2026,
            revision_no=1,
            defaults={"name": "2026 年预算", "status": BudgetCycle.Status.OPEN},
        )
        if cycle.status == BudgetCycle.Status.SETUP:
            cycle.status = BudgetCycle.Status.OPEN
            cycle.save(update_fields=["status"])

        admin, _ = User.objects.get_or_create(
            username="admin",
            defaults={"role": "ADMIN", "is_staff": True, "is_superuser": True},
        )
        admin.set_password("admin123")
        admin.role = "ADMIN"
        admin.project = None
        admin.is_staff = True
        admin.is_superuser = True
        admin.save()

        for code in ["P001", "P002", "P003"]:
            project, _ = Project.objects.get_or_create(code=code, defaults={"name": f"示例项目 {code}"})
            user = User.objects.filter(project=project).first()
            if user is None:
                user, _ = User.objects.get_or_create(
                    username=code.lower(),
                    defaults={"role": "PROJECT", "project": project},
                )
            user.set_password("project123")
            user.role = "PROJECT"
            user.project = project
            user.save()

        self.stdout.write(self.style.SUCCESS(f"demo ready: cycle={cycle.id}"))
