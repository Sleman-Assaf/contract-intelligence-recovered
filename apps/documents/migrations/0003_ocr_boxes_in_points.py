"""OCR boxes were stored in pixels of the 180-dpi render; convert to points.

Only blocks that came from OCR. Native-text blocks were always in points.
"""
from django.db import migrations

SCALE = 72 / 180
OCR_METHODS = ("ocr", "ocr_verified_by_vision")


def forwards(apps, schema_editor):
    Block = apps.get_model("documents", "Block")
    for block in Block.objects.filter(extraction_method__in=OCR_METHODS).exclude(bbox_x0=None):
        block.bbox_x0 *= SCALE
        block.bbox_y0 *= SCALE
        block.bbox_x1 *= SCALE
        block.bbox_y1 *= SCALE
        block.save(update_fields=["bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1"])


def backwards(apps, schema_editor):
    Block = apps.get_model("documents", "Block")
    for block in Block.objects.filter(extraction_method__in=OCR_METHODS).exclude(bbox_x0=None):
        block.bbox_x0 /= SCALE
        block.bbox_y0 /= SCALE
        block.bbox_x1 /= SCALE
        block.bbox_y1 /= SCALE
        block.save(update_fields=["bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1"])


class Migration(migrations.Migration):
    dependencies = [("documents", "0002_sourcefile_round_note_sourcefile_round_number_and_more")]
    operations = [migrations.RunPython(forwards, backwards)]
