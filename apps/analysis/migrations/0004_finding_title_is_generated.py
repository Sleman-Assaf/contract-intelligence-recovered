from django.db import migrations, models


def flag_existing_generated_titles(apps, schema_editor):
    """Backfill rows written before the flag existed.

    The composed title has always had exactly one shape, so matching its suffix
    is reliable here. A model's own deviation wording never ends this way, and
    if one somehow did, the only consequence is that its title is re-rendered
    from the category — nothing is lost.
    """
    Finding = apps.get_model("analysis", "Finding")
    Finding.objects.filter(
        title__endswith=": deviation from AJi standard"
    ).update(title_is_generated=True)


def unflag(apps, schema_editor):
    Finding = apps.get_model("analysis", "Finding")
    Finding.objects.update(title_is_generated=False)


class Migration(migrations.Migration):

    dependencies = [
        ("analysis", "0003_missingclause_absence_confirmed_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="finding",
            name="title_is_generated",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(flag_existing_generated_titles, unflag),
    ]
