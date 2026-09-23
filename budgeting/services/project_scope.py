"""Annual plan membership is stable even after an account/hotel is deactivated."""
from budgeting.models import Project


def cycle_projects(cycle):
    if cycle is None:
        return Project.objects.none()
    if cycle.plan_id:
        return Project.objects.filter(planproject__plan_id=cycle.plan_id).order_by('code')
    return Project.objects.filter(is_active=True).order_by('code')
