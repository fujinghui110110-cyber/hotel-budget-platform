import re

from django.db import migrations, models


def backfill(apps, schema_editor):
    Value = apps.get_model('budgeting', 'NormalizedValue')
    pending = []
    for value in Value.objects.select_related('upload__cycle').iterator(chunk_size=1000):
        period = value.period
        historical = re.fullmatch(r'([AFB])(20\d{2})(?:M(0[1-9]|1[0-2]))?', period)
        if historical:
            value.data_year = int(historical[2])
            value.data_kind = {'A': 'ACTUAL', 'F': 'FORECAST', 'B': 'BUDGET'}[historical[1]]
            value.month = int(historical[3]) if historical[3] else None
        elif period in ('YEAR', 'FY') or re.fullmatch(r'0[1-9]|1[0-2]', period):
            value.data_year = value.upload.cycle.budget_year
            value.data_kind = 'BUDGET'
            value.month = int(period) if period.isdigit() else None
        else:
            continue
        pending.append(value)
        if len(pending) >= 1000:
            Value.objects.bulk_update(pending, ['data_year', 'data_kind', 'month'])
            pending.clear()
    if pending:
        Value.objects.bulk_update(pending, ['data_year', 'data_kind', 'month'])


class Migration(migrations.Migration):
    dependencies = [('budgeting', '0005_cockpit_dimensions')]
    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AddConstraint('normalizedvalue', models.CheckConstraint(condition=models.Q(month__isnull=True) | models.Q(month__gte=1, month__lte=12), name='norm_month_range')),
        migrations.AddConstraint('normalizedvalue', models.UniqueConstraint(fields=['upload', 'report_code', 'row_code', 'data_year', 'data_kind', 'month'], condition=models.Q(month__isnull=False, data_year__isnull=False), name='uniq_norm_month_dimensions')),
    ]
