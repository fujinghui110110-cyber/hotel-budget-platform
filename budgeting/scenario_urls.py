from django.urls import path

from budgeting import scenario_views


urlpatterns = [
    path("management/scenarios/", scenario_views.scenario_list, name="scenario_list"),
    path("management/scenarios/", scenario_views.management_scenarios, name="management_scenarios"),
    path("management/scenarios/new/", scenario_views.scenario_new, name="scenario_new"),
    path("management/scenarios/new/", scenario_views.management_scenario_new, name="management_scenario_new"),
    path("management/scenarios/<uuid:scenario_id>/", scenario_views.scenario_detail, name="scenario_detail"),
    path("management/scenarios/<uuid:scenario_id>/", scenario_views.management_scenario_detail, name="management_scenario_detail"),
    path("management/scenarios/<uuid:scenario_id>/calculate/", scenario_views.scenario_calculate, name="scenario_calculate"),
    path("management/scenarios/<uuid:scenario_id>/calculate/", scenario_views.management_scenario_calculate, name="management_scenario_calculate"),
    path("management/scenarios/<uuid:scenario_id>/issue/", scenario_views.scenario_issue, name="scenario_issue"),
    path("management/scenarios/<uuid:scenario_id>/issue/", scenario_views.management_scenario_issue, name="management_scenario_issue"),
    path("management/scenarios/create/", scenario_views.scenario_create, name="scenario_create"),
]
