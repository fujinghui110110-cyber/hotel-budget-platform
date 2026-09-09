from django.urls import path

from budgeting import cockpit_views as views


urlpatterns = [
    path("management/cockpit/", views.management_dashboard, name="cockpit_dashboard"),
    path("management/cockpit/trend/", views.management_trend, name="cockpit_trend"),
    path("management/cockpit/trend/data/", views.cockpit_trend_data, name="cockpit_trend_data"),
    path("management/cockpit/export/", views.cockpit_export, name="cockpit_export"),
    path("management/cockpit/drilldown/", views.cockpit_drilldown, name="cockpit_drilldown"),
    path("management/cockpit/projects/<int:project_id>/", views.cockpit_project, name="cockpit_project"),
    path("management/cockpit/project/<int:project_id>/", views.cockpit_project, name="cockpit_project_detail"),
    path("questions/", views.cockpit_questions, name="cockpit_questions"),
    path("questions/new/", views.cockpit_question, name="cockpit_question_new"),
    path("questions/<int:question_id>/", views.cockpit_question, name="cockpit_question"),
]
