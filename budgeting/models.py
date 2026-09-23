import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


REPORTS = {
    "PL_TOTAL_WINE": "酒店损益总表（含名酒）",
    "PL_TOTAL_NOWINE": "酒店损益总表（不含名酒）",
    "PL_ZZ_WINE": "损益表（含名酒）（拆中智）",
    "PL_ZZ_NOWINE": "损益表（不含名酒）（拆中智）",
}


class Project(models.Model):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "管理端"
        PROJECT = "PROJECT", "项目端"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.PROJECT)
    project = models.OneToOneField(Project, null=True, blank=True, on_delete=models.PROTECT)

    def clean(self):
        if self.role == self.Role.PROJECT and not self.project_id:
            raise ValidationError("项目账号必须绑定且只能绑定一个项目。")
        if self.role == self.Role.ADMIN and self.project_id:
            raise ValidationError("管理账号不能绑定项目。")

    @property
    def is_admin_role(self):
        return self.role == self.Role.ADMIN or self.is_superuser


class BudgetPlan(models.Model):
    budget_year = models.PositiveIntegerField(unique=True)
    revision_token = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, default="OPEN")
    created_at = models.DateTimeField(auto_now_add=True)


class PlanProject(models.Model):
    plan = models.ForeignKey(BudgetPlan, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["plan", "project"], name="uniq_plan_project")]


class ImmutableHistoryQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("历史版本不可原地修改，请创建修订。")

    def delete(self):
        raise ValidationError("历史版本不可删除。")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("历史版本不可原地修改，请创建修订。")

    def bulk_create(self, objs, **kwargs):
        objs = list(objs)
        if self.model.__name__ == "HistoryBaselineValue":
            from budgeting.services.plan_history import VALUE_FIELDS, _canonical, _hash
            grouped = {}
            for obj in objs:
                grouped.setdefault(obj.baseline_id, []).append(obj)
            for baseline_id, rows in grouped.items():
                baseline = HistoryBaseline.objects.get(pk=baseline_id)
                values = [{key: getattr(row, key) for key in VALUE_FIELDS} for row in rows]
                if self.filter(baseline_id=baseline_id).exists() or _hash(_canonical(values)) != baseline.content_hash:
                    raise ValidationError("历史值只能按确认时完整哈希创建一次。")
        return super().bulk_create(objs, **kwargs)


class ImmutableHistory(models.Model):
    objects = ImmutableHistoryQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("历史版本不可原地修改，请创建修订。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("历史版本不可删除。")


class HistoryBaseline(ImmutableHistory):
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    content_hash = models.CharField(max_length=64)
    source_identity = models.JSONField(default=dict)
    reason = models.TextField()
    confirmed_by = models.ForeignKey(User, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(auto_now_add=True)
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    differences = models.JSONField(default=list)
    status = models.CharField(max_length=12, default="LOCKED")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "revision"], name="uniq_history_revision")]


class HistoryBaselineValue(ImmutableHistory):
    def save(self, *args, **kwargs):
        raise ValidationError("历史值必须通过确认服务一次性创建，不可单独增改。")

    baseline = models.ForeignKey(HistoryBaseline, on_delete=models.PROTECT, related_name="values")
    report_code = models.CharField(max_length=40)
    row_code = models.CharField(max_length=120)
    data_year = models.PositiveIntegerField()
    data_kind = models.CharField(max_length=12)
    period = models.CharField(max_length=40)
    unit = models.CharField(max_length=20)
    value_int = models.BigIntegerField()
    ratio_num = models.BigIntegerField(null=True, blank=True)
    ratio_den = models.BigIntegerField(null=True, blank=True)
    source_sheet = models.CharField(max_length=120, blank=True)
    source_cell = models.CharField(max_length=20, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["baseline", "report_code", "row_code", "data_year", "data_kind", "period"], name="uniq_baseline_value")]


class PlanHistoryBinding(ImmutableHistory):
    plan = models.ForeignKey(BudgetPlan, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    baseline = models.ForeignKey(HistoryBaseline, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    binding_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["plan", "project", "revision"], name="uniq_plan_history_revision")]


class BudgetCycle(models.Model):
    plan = models.ForeignKey(BudgetPlan, null=True, blank=True, on_delete=models.PROTECT)
    class Status(models.TextChoices):
        SETUP = "SETUP", "设置中"
        OPEN = "OPEN", "开放上传"
        ADJUSTING = "ADJUSTING", "管理调整"
        READY_TO_FREEZE = "READY_TO_FREEZE", "待冻结"
        FROZEN = "FROZEN", "已冻结"

    SETUP = Status.SETUP
    OPEN = Status.OPEN
    ADJUSTING = Status.ADJUSTING
    READY_TO_FREEZE = Status.READY_TO_FREEZE
    FROZEN = Status.FROZEN
    name = models.CharField(max_length=120)
    budget_year = models.PositiveIntegerField()
    source_budget_year = models.PositiveIntegerField(null=True, blank=True)
    revision_no = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.SETUP)
    template = models.ForeignKey("TemplateVersion", null=True, blank=True, on_delete=models.PROTECT)
    p1_threshold_cents = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    frozen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["budget_year", "revision_no"], name="uniq_cycle_revision")]

    def __str__(self):
        return f"{self.name} R{self.revision_no}"


class TemplateVersion(models.Model):
    version = models.CharField(max_length=40, unique=True)
    budget_year = models.PositiveIntegerField()
    file_path = models.CharField(max_length=500)
    manifest_path = models.CharField(max_length=500)
    formula_manifest_hash = models.CharField(max_length=64)
    rule_version = models.CharField(max_length=40, default="R1")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.version


class UploadVersion(models.Model):
    history_binding = models.ForeignKey(PlanHistoryBinding, null=True, blank=True, on_delete=models.PROTECT)
    history_stale = models.BooleanField(default=False)
    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "已接收"
        PROCESSING = "PROCESSING", "处理中"
        REJECTED = "REJECTED", "已拒绝"
        VALIDATED = "VALIDATED", "已校验"
        SUBMITTED = "SUBMITTED", "已提交"
        APPROVED = "APPROVED", "已批准"
        SUPERSEDED = "SUPERSEDED", "已替换"

    RECEIVED = Status.RECEIVED
    PROCESSING = Status.PROCESSING
    REJECTED = Status.REJECTED
    VALIDATED = Status.VALIDATED
    SUBMITTED = Status.SUBMITTED
    APPROVED = Status.APPROVED
    SUPERSEDED = Status.SUPERSEDED

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    cycle = models.ForeignKey(BudgetCycle, on_delete=models.PROTECT)
    template = models.ForeignKey(TemplateVersion, null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.RECEIVED)
    original_name = models.CharField(max_length=255, default="")
    original_path = models.CharField(max_length=500)
    recalculated_path = models.CharField(max_length=500, blank=True)
    sha256 = models.CharField(max_length=64)
    note = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processing_current_run = models.ForeignKey(
        "ProcessingRun",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="current_for_uploads",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["project", "cycle", "status"])]


class ProjectCycle(models.Model):
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    cycle = models.ForeignKey(BudgetCycle, on_delete=models.PROTECT)
    current_upload = models.OneToOneField(UploadVersion, null=True, blank=True, on_delete=models.PROTECT)
    is_open = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "cycle"], name="uniq_project_cycle")]


class ProcessingJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "排队中"
        RUNNING = "RUNNING", "运行中"
        DONE = "DONE", "完成"
        FAILED = "FAILED", "失败"

    idempotency_key = models.CharField(max_length=80, unique=True)
    upload = models.ForeignKey(UploadVersion, on_delete=models.CASCADE)
    processing_run = models.OneToOneField(
        "ProcessingRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="job",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ProcessingRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "排队中"
        RUNNING = "RUNNING", "运行中"
        SUCCEEDED = "SUCCEEDED", "成功"
        FAILED = "FAILED", "失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    upload = models.ForeignKey(UploadVersion, on_delete=models.CASCADE, related_name="processing_runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    rule_version = models.CharField(max_length=40, default="")
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["upload", "status", "created_at"])]


class HistoricalImport(models.Model):
    """Administrator-owned historical source, independent of budget revisions."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    data_year = models.PositiveIntegerField()
    data_kind = models.CharField(max_length=12, choices=[("ACTUAL", "实际"), ("FORECAST", "预测")])
    report_code = models.CharField(max_length=40, choices=list(REPORTS.items()))
    money_unit = models.CharField(max_length=8, default="yuan")
    original_name = models.CharField(max_length=255)
    original_path = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64)
    proposal = models.JSONField(default=dict)
    active = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(
            fields=["project", "data_year", "data_kind", "report_code"],
            condition=Q(active=True), name="unique_active_historical_source")]


class HistoricalValue(models.Model):
    import_batch = models.ForeignKey(HistoricalImport, related_name="values", on_delete=models.CASCADE)
    row_code = models.CharField(max_length=120)
    row_label = models.CharField(max_length=240)
    period = models.CharField(max_length=40)
    month = models.PositiveSmallIntegerField(null=True, blank=True)
    unit = models.CharField(max_length=20)
    value_int = models.BigIntegerField(default=0)
    ratio_num = models.BigIntegerField(null=True, blank=True)
    ratio_den = models.BigIntegerField(null=True, blank=True)
    source_sheet = models.CharField(max_length=120)
    source_cell = models.CharField(max_length=20)
    source_formula = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["import_batch", "row_code", "period"], name="unique_historical_cell")]


class NormalizedValue(models.Model):
    class Unit(models.TextChoices):
        MONEY = "MONEY", "金额"
        COUNT = "COUNT", "数量"
        RATIO = "RATIO", "比率"

    MONEY = Unit.MONEY
    COUNT = Unit.COUNT
    RATIO = Unit.RATIO

    upload = models.ForeignKey(UploadVersion, on_delete=models.CASCADE)
    processing_run = models.ForeignKey(
        ProcessingRun,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="normalized_values",
    )
    history_import = models.ForeignKey(HistoricalImport, null=True, blank=True, on_delete=models.PROTECT)
    report_code = models.CharField(max_length=40)
    row_code = models.CharField(max_length=120)
    row_label = models.CharField(max_length=240, blank=True)
    period = models.CharField(max_length=40)
    data_year = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    data_kind = models.CharField(max_length=12, blank=True, choices=[("ACTUAL", "实际"), ("FORECAST", "预测"), ("BUDGET", "预算")])
    month = models.PositiveSmallIntegerField(null=True, blank=True)
    unit = models.CharField(max_length=20, choices=Unit.choices)
    value_int = models.BigIntegerField(default=0)
    ratio_num = models.BigIntegerField(null=True, blank=True)
    ratio_den = models.BigIntegerField(null=True, blank=True)
    source_sheet = models.CharField(max_length=120)
    source_cell = models.CharField(max_length=20)
    source_formula = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["upload", "report_code", "row_code", "period"], name="uniq_norm_value"),
            models.CheckConstraint(condition=Q(month__isnull=True) | Q(month__gte=1, month__lte=12), name="norm_month_range"),
            models.UniqueConstraint(fields=["upload", "report_code", "row_code", "data_year", "data_kind", "month"], condition=Q(month__isnull=False, data_year__isnull=False), name="uniq_norm_month_dimensions"),
        ]


class ManagementQuestion(models.Model):
    upload = models.ForeignKey(UploadVersion, on_delete=models.PROTECT)
    report_code = models.CharField(max_length=40)
    row_code = models.CharField(max_length=120)
    period = models.CharField(max_length=40)
    snapshot = models.JSONField(default=dict)
    body = models.TextField()
    status = models.CharField(max_length=12, default="OPEN", choices=[("OPEN", "待回复"), ("ANSWERED", "已回复"), ("CLOSED", "已关闭")])
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class QuestionReply(models.Model):
    question = models.ForeignKey(ManagementQuestion, on_delete=models.PROTECT, related_name="replies")
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class BudgetScenario(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    baseline = models.ForeignKey(UploadVersion, on_delete=models.PROTECT)
    name = models.CharField(max_length=120)
    inputs = models.JSONField(default=dict)
    results = models.JSONField(default=dict)
    rule_version = models.CharField(max_length=40, default="FIXED_COST_V2")
    status = models.CharField(max_length=12, default="DRAFT", choices=[("DRAFT", "草稿"), ("READY", "已测算"), ("ISSUED", "已下发"), ("FAILED", "失败")])
    error = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    batch = models.ForeignKey("AdjustmentBatch", null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ValidationRun(models.Model):
    upload = models.ForeignKey(UploadVersion, on_delete=models.CASCADE)
    processing_run = models.ForeignKey(
        ProcessingRun,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="validation_runs",
    )
    rule_version = models.CharField(max_length=40)
    passed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class ValidationIssue(models.Model):
    class Severity(models.TextChoices):
        P0 = "P0", "阻断"
        P1 = "P1", "确认"
        P2 = "P2", "提示"

    run = models.ForeignKey(ValidationRun, on_delete=models.CASCADE, related_name="issues")
    severity = models.CharField(max_length=2, choices=Severity.choices)
    code = models.CharField(max_length=60)
    message = models.TextField()
    location = models.CharField(max_length=200, blank=True)
    actual_value = models.CharField(max_length=120, blank=True)
    expected_value = models.CharField(max_length=120, blank=True)
    acknowledged = models.BooleanField(default=False)
    acknowledgement_note = models.TextField(blank=True)


class AdjustmentBatch(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "草稿"
        ISSUED = "ISSUED", "已下发"
        COMPLETED = "COMPLETED", "已完成"
        CANCELLED = "CANCELLED", "已取消"

    DRAFT = Status.DRAFT
    ISSUED = Status.ISSUED
    COMPLETED = Status.COMPLETED
    CANCELLED = Status.CANCELLED

    cycle = models.ForeignKey(BudgetCycle, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.PROTECT)
    driver = models.CharField(max_length=20, default="")
    driver_label = models.CharField(max_length=60, default="")
    from_value = models.DecimalField(max_digits=24, decimal_places=4, null=True, blank=True)
    to_value = models.DecimalField(max_digits=24, decimal_places=4, null=True, blank=True)
    target_room_rev_cents = models.BigIntegerField(default=0)
    cascade = models.JSONField(default=dict, blank=True)
    report_code = models.CharField(max_length=40)
    row_code = models.CharField(max_length=120)
    period = models.CharField(max_length=40)
    baseline_total_cents = models.BigIntegerField(default=0)
    delta_cents = models.BigIntegerField()
    reason = models.TextField()
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    issued_at = models.DateTimeField(null=True, blank=True)

    @property
    def row_label(self):
        label = NormalizedValue.objects.filter(
            upload__cycle=self.cycle, report_code=self.report_code, row_code=self.row_code
        ).values_list("row_label", flat=True).first()
        return label or self.row_code


class AdjustmentLine(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "待项目重传"
        CONFIRMED = "CONFIRMED", "已确认"
        CANCELLED = "CANCELLED", "已取消"

    OPEN = Status.OPEN
    CONFIRMED = Status.CONFIRMED
    CANCELLED = Status.CANCELLED

    batch = models.ForeignKey(AdjustmentBatch, on_delete=models.CASCADE, related_name="lines")
    cycle = models.ForeignKey(BudgetCycle, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    report_code = models.CharField(max_length=40)
    row_code = models.CharField(max_length=120)
    period = models.CharField(max_length=40)
    baseline_cents = models.BigIntegerField(default=0)
    weight = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    allocated_delta_cents = models.BigIntegerField(default=0)
    target_cents = models.BigIntegerField(default=0)
    latest_upload = models.ForeignKey(UploadVersion, null=True, blank=True, on_delete=models.PROTECT)
    latest_value_cents = models.BigIntegerField(null=True, blank=True)
    difference_cents = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cycle", "project", "report_code", "row_code", "period"],
                condition=Q(status="OPEN"),
                name="uniq_open_adjustment_target",
            ),
        ]


class FreezeSnapshot(models.Model):
    history_bindings = models.JSONField(default=dict)
    class Status(models.TextChoices):
        STAGING = "STAGING", "生成中"
        COMPLETE = "COMPLETE", "完成"
        FAILED = "FAILED", "失败"

    STAGING = Status.STAGING
    COMPLETE = Status.COMPLETE
    FAILED = Status.FAILED

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cycle = models.ForeignKey(BudgetCycle, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.STAGING)
    directory = models.CharField(max_length=500, blank=True)
    manifest_path = models.CharField(max_length=500, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class SnapshotArtifact(models.Model):
    snapshot = models.ForeignKey(FreezeSnapshot, on_delete=models.CASCADE, related_name="artifacts")
    kind = models.CharField(max_length=40)
    relative_path = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField(default=0)


class AuditEvent(models.Model):
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=80)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL)
    cycle = models.ForeignKey(BudgetCycle, null=True, blank=True, on_delete=models.SET_NULL)
    upload = models.ForeignKey(UploadVersion, null=True, blank=True, on_delete=models.SET_NULL)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ManagementMetricHistoryQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError('已确认历史指标不可覆盖，请上传新修订。')

    def delete(self):
        raise ValueError('已确认历史指标不可删除。')


class ManagementMetricHistoryRecord(models.Model):
    objects = ManagementMetricHistoryQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('已确认历史指标不可覆盖，请上传新修订。')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('已确认历史指标不可删除。')


class ManagementMetricBatch(ManagementMetricHistoryRecord):
    """An append-only administrator-confirmed analytical history revision."""
    original = models.FileField(upload_to='management_metrics/%Y/%m/')
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    money_unit = models.CharField(max_length=8)
    data_kind = models.CharField(max_length=12)
    report_code = models.CharField(max_length=40)
    reason = models.TextField()
    created_by = models.ForeignKey('User', on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ManagementMetricValue(ManagementMetricHistoryRecord):
    batch = models.ForeignKey(ManagementMetricBatch, on_delete=models.PROTECT, related_name='values')
    project = models.ForeignKey('IndicatorProject', on_delete=models.PROTECT)
    metric = models.CharField(max_length=64)
    year = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField()
    value = models.DecimalField(max_digits=28, decimal_places=10, null=True)
    source_sheet = models.CharField(max_length=120)
    source_cell = models.CharField(max_length=20)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['batch', 'project', 'metric', 'year', 'month'], name='unique_management_metric_value'),
            models.CheckConstraint(condition=models.Q(month__gte=0, month__lte=12), name='management_metric_month_range'),
        ]


class IndicatorProject(models.Model):
    name = models.CharField(max_length=120, unique=True)
    project = models.OneToOneField(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name='indicator_project')

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.name


class SpecialIndicatorBatch(models.Model):
    year = models.PositiveIntegerField()
    data_type = models.CharField(max_length=12, choices=[('ACTUAL', '实际'), ('FORECAST', '预测'), ('BUDGET', '预算')])
    cycle = models.ForeignKey(BudgetCycle, null=True, blank=True, on_delete=models.PROTECT)
    unit = models.CharField(max_length=12)
    original = models.FileField(upload_to='special_indicators/%Y/%m/')
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    created_by = models.ForeignKey('User', null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['year', 'data_type'], condition=models.Q(active=True, cycle__isnull=True), name='unique_active_special_actual'),
            models.UniqueConstraint(fields=['year', 'data_type', 'cycle'], condition=models.Q(active=True, cycle__isnull=False), name='unique_active_special_budget'),
        ]


class SpecialIndicatorValue(models.Model):
    batch = models.ForeignKey(SpecialIndicatorBatch, on_delete=models.CASCADE, related_name='values')
    project = models.ForeignKey(IndicatorProject, on_delete=models.PROTECT)
    indicator = models.CharField(max_length=20)
    month = models.PositiveSmallIntegerField()
    value = models.DecimalField(max_digits=24, decimal_places=4, null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['batch', 'project', 'indicator', 'month'], name='unique_special_indicator_month')]


class ImmutableTargetQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("目标记录不可覆盖，请下发新修订。")

    def delete(self):
        raise ValidationError("目标记录不可删除，请显式撤销。")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("目标记录不可覆盖，请下发新修订。")


class ImmutableTarget(models.Model):
    objects = ImmutableTargetQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("目标记录不可覆盖，请下发新修订。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("目标记录不可删除，请显式撤销。")


class TargetSet(ImmutableTarget):
    """Append-only complete target revision; an empty revision explicitly revokes it."""
    plan = models.ForeignKey(BudgetPlan, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    origin_cycle = models.ForeignKey(BudgetCycle, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    issued_sequence = models.PositiveIntegerField()
    supersedes = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    reason = models.TextField()
    source = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['plan', 'project', 'revision'], name='uniq_plan_project_target_revision')]


class TargetConstraint(ImmutableTarget):
    target_set = models.ForeignKey(TargetSet, on_delete=models.PROTECT, related_name='constraints')
    report_code = models.CharField(max_length=40)
    row_code = models.CharField(max_length=120)
    period = models.CharField(max_length=40)
    unit = models.CharField(max_length=20)
    comparator = models.CharField(max_length=2)
    target_int = models.BigIntegerField()
    metric_kind = models.CharField(max_length=20)
    sign_multiplier = models.SmallIntegerField(default=1)
    evidence = models.TextField()
    rule_version = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['target_set', 'report_code', 'row_code', 'period', 'comparator'], name='uniq_target_scope_comparator')]


class TargetEvaluation(ImmutableTarget):
    upload = models.ForeignKey(UploadVersion, on_delete=models.PROTECT)
    target_set = models.ForeignKey(TargetSet, null=True, blank=True, on_delete=models.PROTECT)
    context = models.JSONField(default=dict)
    status = models.CharField(max_length=24)
    created_at = models.DateTimeField(auto_now_add=True)


class TargetEvaluationItem(ImmutableTarget):
    evaluation = models.ForeignKey(TargetEvaluation, on_delete=models.PROTECT, related_name='items')
    constraint = models.ForeignKey(TargetConstraint, on_delete=models.PROTECT)
    status = models.CharField(max_length=24)
    actual_int = models.BigIntegerField(null=True)
    favorable_delta = models.BigIntegerField(null=True)
    shortfall = models.BigIntegerField(null=True)


class BudgetTemplateFile(models.Model):
    """Immutable distributed workbook, separate from the import rule template."""
    cycle = models.ForeignKey(BudgetCycle, on_delete=models.PROTECT, related_name='distributed_templates')
    name = models.CharField(max_length=120)
    original_name = models.CharField(max_length=255)
    file_path = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
