from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0066_script_inputs"),
    ]

    operations = [
        migrations.AddField(
            model_name="run",
            name="result",
            field=models.JSONField(
                blank=True,
                default=None,
                help_text="JSON object the script set with pyrunner_run.set_result() (secrets masked).",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="run",
            name="progress",
            field=models.JSONField(
                blank=True,
                default=None,
                help_text="Latest pyrunner_run.progress() update: {pct, message, updated_at}.",
                null=True,
            ),
        ),
    ]
