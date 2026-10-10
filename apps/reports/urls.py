from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("reports/", views.ReportIndexView.as_view(), name="index"),
    path("reports/<uuid:pk>/", views.ReportDetailView.as_view(), name="detail"),
    path("reports/<uuid:pk>/pdf/", views.report_pdf, name="pdf"),
    path("reports/<uuid:pk>/redline/", views.report_redline, name="redline"),
]
