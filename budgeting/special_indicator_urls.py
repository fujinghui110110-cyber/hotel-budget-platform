from django.urls import path
from budgeting import special_indicator_views as views

urlpatterns = [
    path('', views.comparison, name='special_indicators'),
    path('import/', views.import_indicators, name='special_indicator_import'),
    path('confirm/', views.confirm_indicators, name='special_indicator_confirm'),
    path('template/', views.template_download, name='special_indicator_template'),
    path('sources/<int:batch_id>/', views.source_download, name='special_indicator_source'),
]
