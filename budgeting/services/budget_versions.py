from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Max

from budgeting.models import BudgetCycle, BudgetPlan, PlanProject, Project, ProjectCycle, TemplateVersion, UploadVersion
from budgeting.services.workflow import audit


REPORTABLE_UPLOAD_STATUSES = (
    UploadVersion.Status.VALIDATED,
    UploadVersion.Status.SUBMITTED,
    UploadVersion.Status.APPROVED,
)
UPLOADABLE_CYCLE_STATUSES = (
    BudgetCycle.Status.OPEN,
    BudgetCycle.Status.ADJUSTING,
)


@dataclass(frozen=True)
class BudgetVersionProjectRow:
    project: Project
    latest_upload: UploadVersion | None
    report_upload: UploadVersion | None

    @property
    def has_uploaded(self) -> bool:
        return self.latest_upload is not None

    @property
    def can_view_report(self) -> bool:
        return self.report_upload is not None


def version_label(cycle: BudgetCycle) -> str:
    return f"{cycle.budget_year} 年预算 R{cycle.revision_no}"


def budget_version_accepts_uploads(cycle: BudgetCycle) -> bool:
    if cycle.status not in UPLOADABLE_CYCLE_STATUSES:
        return False
    project_cycles = ProjectCycle.objects.filter(cycle=cycle)
    if not project_cycles.exists():
        return True
    return project_cycles.filter(is_open=True).exists()


def project_open_cycle(project: Project | int) -> BudgetCycle | None:
    project_id = project.pk if isinstance(project, Project) else project
    cycle = (
        BudgetCycle.objects.filter(
            status__in=UPLOADABLE_CYCLE_STATUSES,
            projectcycle__project_id=project_id,
            projectcycle__is_open=True,
        )
        .order_by("-budget_year", "-revision_no", "-created_at")
        .first()
    )
    if cycle is not None:
        return cycle
    return (
        BudgetCycle.objects.filter(
            status__in=UPLOADABLE_CYCLE_STATUSES,
            projectcycle__isnull=True,
        )
        .order_by("-budget_year", "-revision_no", "-created_at")
        .first()
    )


def _real_uploads(project: Project, cycle: BudgetCycle):
    return UploadVersion.objects.filter(project=project, cycle=cycle).exclude(
        note__startswith="由冻结周期 "
    )


def selected_project_upload(project: Project, cycle: BudgetCycle) -> UploadVersion | None:
    return (
        _real_uploads(project, cycle)
        .filter(status__in=REPORTABLE_UPLOAD_STATUSES)
        .order_by("-created_at", "-id")
        .first()
    )


def budget_version_rows(cycle: BudgetCycle) -> list[BudgetVersionProjectRow]:
    rows = []
    projects = Project.objects.filter(planproject__plan_id=cycle.plan_id) if cycle.plan_id else Project.objects.filter(is_active=True)
    for project in projects.order_by("code"):
        latest = _real_uploads(project, cycle).order_by("-created_at", "-id").first()
        report_upload = selected_project_upload(project, cycle)
        rows.append(
            BudgetVersionProjectRow(
                project=project,
                latest_upload=latest,
                report_upload=report_upload,
            )
        )
    return rows


@transaction.atomic
def create_and_open_budget_version(
    *,
    budget_year: int,
    actor=None,
    name: str = "",
    template: TemplateVersion | None = None,
    source_budget_year: int | None = None,
) -> BudgetCycle:
    if budget_year < 2000 or budget_year > 2200:
        raise ValueError("请输入有效的预算年度。")
    if source_budget_year is not None and not 2000 <= source_budget_year < budget_year:
        raise ValueError("演练原表年度必须早于预算年度，且不早于 2000 年。")

    latest_revision = (
        BudgetCycle.objects.select_for_update()
        .filter(budget_year=budget_year)
        .aggregate(value=Max("revision_no"))["value"]
        or 0
    )
    if template is None:
        template = (
            TemplateVersion.objects.filter(is_active=True, budget_year=budget_year)
            .order_by("-created_at")
            .first()
        )
    if template is None:
        raise ValueError("请先发布可用的统一预算模板。")

    old_editable = BudgetCycle.objects.select_for_update().filter(
        budget_year=budget_year,
        status__in=UPLOADABLE_CYCLE_STATUSES,
    )
    old_ids = list(old_editable.values_list("pk", flat=True))
    old_editable.filter(status=BudgetCycle.Status.OPEN).update(
        status=BudgetCycle.Status.ADJUSTING
    )
    if old_ids:
        ProjectCycle.objects.filter(cycle_id__in=old_ids).update(is_open=False)

    from budgeting.services.plan_history import ensure_plan
    existing_cycle = BudgetCycle.objects.filter(budget_year=budget_year).first()
    if existing_cycle:
        plan = ensure_plan(existing_cycle)
    else:
        plan, created = BudgetPlan.objects.get_or_create(budget_year=budget_year)
        if created:
            PlanProject.objects.bulk_create([
                PlanProject(plan=plan, project=project)
                for project in Project.objects.filter(is_active=True)
            ])
    revision_no = latest_revision + 1
    cycle = BudgetCycle.objects.create(
        name=(name or f"{budget_year} 年度预算").strip(),
        source_budget_year=source_budget_year,
        budget_year=budget_year,
        plan=plan,
        revision_no=revision_no,
        status=BudgetCycle.Status.OPEN,
        template=template,
    )
    ProjectCycle.objects.bulk_create(
        [
            ProjectCycle(project=project, cycle=cycle, is_open=True)
            for project in (Project.objects.filter(planproject__plan_id=cycle.plan_id) if cycle.plan_id else Project.objects.filter(is_active=True)).order_by("pk")
        ]
    )
    audit(
        actor,
        "BUDGET_VERSION_OPENED",
        "BudgetCycle",
        cycle.pk,
        {"budget_year": budget_year, "revision_no": revision_no},
        cycle=cycle,
    )
    return cycle


@transaction.atomic
def close_budget_version(cycle: BudgetCycle, *, actor=None) -> BudgetCycle:
    cycle = BudgetCycle.objects.select_for_update().get(pk=cycle.pk)
    if not budget_version_accepts_uploads(cycle):
        raise ValueError("只能关闭正在开放的预算版本。")
    cycle.status = BudgetCycle.Status.ADJUSTING
    cycle.save(update_fields=["status"])
    project_cycles = ProjectCycle.objects.filter(cycle=cycle)
    if project_cycles.exists():
        project_cycles.update(is_open=False)
    else:
        ProjectCycle.objects.bulk_create(
            [
                ProjectCycle(project=project, cycle=cycle, is_open=False)
                for project in (Project.objects.filter(planproject__plan_id=cycle.plan_id) if cycle.plan_id else Project.objects.filter(is_active=True)).order_by("pk")
            ]
        )
    audit(
        actor,
        "BUDGET_VERSION_CLOSED",
        "BudgetCycle",
        cycle.pk,
        {"budget_year": cycle.budget_year, "revision_no": cycle.revision_no},
        cycle=cycle,
    )
    return cycle
