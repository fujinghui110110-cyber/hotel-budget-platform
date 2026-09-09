from django.apps import AppConfig
from django.db.backends.signals import connection_created


class BudgetingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "budgeting"

    def ready(self):
        connection_created.connect(enable_sqlite_wal, dispatch_uid="budgeting.sqlite_pragmas")


def enable_sqlite_wal(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA busy_timeout=30000;")
