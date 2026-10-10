from django.urls import path

from . import views

app_name = "contracts"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("contracts/", views.ContractListView.as_view(), name="list"),
    path("search/", views.SearchView.as_view(), name="search"),
    path("search/suggest/", views.search_suggest, name="search_suggest"),
    path("contracts/upload/", views.ContractUploadView.as_view(), name="upload"),
    path("contracts/<uuid:pk>/", views.ContractDetailView.as_view(), name="detail"),
    path("contracts/<uuid:pk>/reanalyse/", views.reanalyse, name="reanalyse"),
    path("contracts/<uuid:pk>/progress/", views.progress, name="progress"),
    path("contracts/<uuid:pk>/rounds/", views.ContractRoundsView.as_view(), name="rounds"),
    path("contracts/<uuid:pk>/rounds/new/", views.new_round, name="new_round"),
    path("files/<uuid:pk>/download/", views.download_source_file, name="download"),
]
