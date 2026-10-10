from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.documents"
    label = "documents"

    def ready(self):
        # Registered here so `manage.py check` reports a machine that cannot
        # read scanned pages, instead of the first upload discovering it.
        from django.core.checks import register

        from .checks import check_ocr_engine

        register(check_ocr_engine)
