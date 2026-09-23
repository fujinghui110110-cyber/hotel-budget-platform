import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("budgeting", "0011_independent_history_cache"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProcessingRun",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("QUEUED", "排队中"), ("RUNNING", "运行中"), ("SUCCEEDED", "成功"), ("FAILED", "失败")], default="QUEUED", max_length=20)),
                ("rule_version", models.CharField(default="", max_length=40)),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("upload", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="processing_runs", to="budgeting.uploadversion")),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddField(
            model_name="uploadversion",
            name="processing_current_run",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="current_for_uploads", to="budgeting.processingrun"),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="processing_run",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="job", to="budgeting.processingrun"),
        ),
        migrations.AddField(
            model_name="validationrun",
            name="processing_run",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="validation_runs", to="budgeting.processingrun"),
        ),
        migrations.AddField(
            model_name="normalizedvalue",
            name="processing_run",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="normalized_values", to="budgeting.processingrun"),
        ),
        migrations.AddIndex(
            model_name="processingrun",
            index=models.Index(fields=["upload", "status", "created_at"], name="budgeting_p_upload__e9c363_idx"),
        ),
    ]
