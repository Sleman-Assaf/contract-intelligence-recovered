from django.urls import path

from . import views

app_name = "workflow"

urlpatterns = [
    path("workflow/", views.WorkflowIndexView.as_view(), name="index"),
    path("workflow/<uuid:pk>/transition/", views.do_transition, name="transition"),
    path("workflow/<uuid:pk>/comment/", views.add_comment, name="comment"),
]
