from django.urls import path

from budgeting import reference_views

urlpatterns = [
    path("management/reference/", reference_views.reference_catalog, name="reference_catalog"),
    path("management/reference/<str:workbook_id>/<str:sheet_id>/", reference_views.reference_sheet, name="reference_sheet"),
]
