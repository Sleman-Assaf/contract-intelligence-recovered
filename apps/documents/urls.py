from django.urls import path

from . import confirm_views, views

app_name = "documents"

urlpatterns = [
    path("documents/<uuid:document_id>/page/<int:page_number>/image/",
         views.page_image, name="page_image"),
    path("documents/<uuid:document_id>/page/<int:page_number>/blocks/",
         views.page_blocks, name="page_blocks"),
    path("documents/<uuid:document_id>/page/<int:page_number>/region/",
         confirm_views.region_image, name="region_image"),
    path("contracts/<uuid:contract_id>/confirm/", confirm_views.confirm_facts, name="confirm"),
]
