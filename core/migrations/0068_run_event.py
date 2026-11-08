from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0067_run_result_progress"),
    ]

    operations = [
        migrations.AddField(
            model_name="run",
            name="event",
            field=models.JSONField(
                blank=True,
                default=None,
                help_text="What started the run: {kind, received_at, ...} (see core.services.run_events).",
                null=True,
            ),
        ),
    ]
