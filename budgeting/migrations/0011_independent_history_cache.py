from django.db import migrations


def discard_budget_history(apps, schema_editor):
    # Historical sources are now administrator-owned. Original workbooks and
    # frozen budgets remain unchanged; only derived, unconfirmed history is removed.
    Value = apps.get_model("budgeting", "NormalizedValue")
    Value.objects.filter(
        data_kind__in=["ACTUAL", "FORECAST"], history_import__isnull=True,
    ).exclude(upload__cycle__status="FROZEN").delete()


class Migration(migrations.Migration):
    dependencies = [("budgeting", "0010_historical_sources")]
    operations = [migrations.RunPython(discard_budget_history, migrations.RunPython.noop)]
