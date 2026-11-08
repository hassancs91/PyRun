from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0073_script_env_vars"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalsettings",
            name="api_writes_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Allow API and MCP tokens to call write endpoints and be granted write permissions.",
            ),
        ),
    ]
