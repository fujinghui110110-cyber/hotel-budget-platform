from django.urls import path

from budgeting import issue_views


urlpatterns = [
    path("issues/<int:issue_id>/", issue_views.issue_explanation, name="issue_explanation"),
    path(
        "management/cycles/<int:cycle_id>/p1-threshold/",
        issue_views.cycle_p1_threshold,
        name="management_p1_threshold",
    ),
]
