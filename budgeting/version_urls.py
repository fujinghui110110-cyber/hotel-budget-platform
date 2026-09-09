from django.urls import path

from budgeting import version_views


urlpatterns = [
    path(
        "management/budget-versions/",
        version_views.budget_versions,
        name="management_budget_versions",
    ),
    path(
        "management/budget-versions/open/",
        version_views.budget_version_open,
        name="management_budget_version_open",
    ),
    path(
        "management/budget-versions/<int:cycle_id>/close/",
        version_views.budget_version_close,
        name="management_budget_version_close",
    ),
    path(
        "management/budget-versions/<int:cycle_id>/projects/<int:project_id>/",
        version_views.budget_version_project,
        name="management_budget_version_project",
    ),
]
