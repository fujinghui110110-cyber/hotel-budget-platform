from django.urls import path
from budgeting.target_views import targets

urlpatterns = [path('plans/<int:plan_id>/projects/<int:project_id>/targets/', targets, name='annual_targets')]
