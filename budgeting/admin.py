from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from budgeting.models import (
    AdjustmentBatch,
    AdjustmentLine,
    AuditEvent,
    BudgetCycle,
    FreezeSnapshot,
    NormalizedValue,
    ProcessingJob,
    Project,
    ProjectCycle,
    SnapshotArtifact,
    TemplateVersion,
    UploadVersion,
    User,
    ValidationIssue,
    ValidationRun,
)


@admin.register(User)
class BudgetUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("预算权限", {"fields": ("role", "project")}),)
    list_display = ("username", "role", "project", "is_staff")


admin.site.register(Project)
admin.site.register(BudgetCycle)
admin.site.register(ProjectCycle)
admin.site.register(TemplateVersion)
admin.site.register(UploadVersion)
admin.site.register(ProcessingJob)
admin.site.register(NormalizedValue)
admin.site.register(ValidationRun)
admin.site.register(ValidationIssue)
admin.site.register(AdjustmentBatch)
admin.site.register(AdjustmentLine)
admin.site.register(FreezeSnapshot)
admin.site.register(SnapshotArtifact)
admin.site.register(AuditEvent)


class ImmutableHistoryAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


from budgeting.models import BudgetPlan, PlanProject, HistoryBaseline, HistoryBaselineValue, PlanHistoryBinding

for history_model in (BudgetPlan, PlanProject, HistoryBaseline, HistoryBaselineValue, PlanHistoryBinding):
    admin.site.register(history_model, ImmutableHistoryAdmin)
admin.site.unregister(AuditEvent)
admin.site.register(AuditEvent, ImmutableHistoryAdmin)
