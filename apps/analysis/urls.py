from django.urls import path

from . import views
from .source_viewer import SourceViewerView

app_name = "analysis"

urlpatterns = [
    path("analysis/<uuid:pk>/", views.AnalysisDetailView.as_view(), name="detail"),
    path("risk-matrix/", views.RiskMatrixView.as_view(), name="risk_matrix"),
    path("comparison/", views.ComparisonView.as_view(), name="comparison"),
    path("findings/<uuid:pk>/decide/", views.decide_finding, name="decide"),
    path("findings/<uuid:pk>/source/", views.finding_source, name="finding_source"),
    path("findings/<uuid:pk>/viewer/", SourceViewerView.as_view(), name="source_viewer"),
    path("advisor/", views.AdvisorView.as_view(), name="advisor"),
    path("advisor/stream/", views.AdvisorStreamView.as_view(), name="advisor_stream"),
    path("advisor/<uuid:pk>/delete/", views.delete_conversation,
         name="advisor_delete"),
    path("advisor/forget/", views.forget_conversations, name="advisor_forget"),
    path("advisor/photo/<uuid:pk>/", views.advisor_photo, name="advisor_photo"),
]
