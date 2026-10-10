from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "recipient", "title", "read_at", "emailed_at")
    list_filter = ("kind",)
    search_fields = ("title", "recipient__email", "dedupe_key")
    date_hierarchy = "created_at"
