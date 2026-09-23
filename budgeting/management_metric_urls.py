from django.urls import path

from budgeting import management_metric_views as views


urlpatterns = [
    path("", views.management_metrics, name="management_metrics"),
    path("import/", views.management_metric_import, name="management_metric_import"),
    path("import/confirm/", views.management_metric_confirm, name="management_metric_confirm"),
    path("sources/<int:batch_id>/", views.management_metric_source, name="management_metric_source"),
    path("export/", views.management_metric_export, name="management_metric_export"),
]
