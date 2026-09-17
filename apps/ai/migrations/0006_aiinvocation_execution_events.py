from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai", "0005_seed_default_profiles")]
    operations = [
        migrations.AddField(model_name="aiinvocation", name="execution_events", field=models.JSONField(default=list)),
        migrations.AddField(model_name="aiinvocation", name="trace_complete", field=models.BooleanField(default=False)),
    ]
