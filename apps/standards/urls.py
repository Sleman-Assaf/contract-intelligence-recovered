from django.urls import path

from . import views

app_name = "standards"

urlpatterns = [
    path("standards/upload/", views.upload, name="upload"),
    path("standards/uploads/<uuid:pk>/", views.review, name="review"),
    path("standards/uploads/<uuid:pk>/retry/", views.retry, name="retry"),
    path("standards/uploads/<uuid:pk>/discard/", views.discard, name="discard"),
    path("standards/versions/<uuid:pk>/approve/", views.approve, name="approve"),
    path("standards/versions/<uuid:pk>/impact/", views.set_impact, name="impact"),
    path("standards/versions/<uuid:pk>/withdraw/", views.withdraw, name="withdraw"),
]
