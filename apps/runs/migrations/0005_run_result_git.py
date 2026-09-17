from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("runs", "0004_processrecord_container_name")]
    operations = [
        migrations.AddField(model_name="run", name="result_branch", field=models.CharField(max_length=100, blank=True)),
        migrations.AddField(model_name="run", name="result_commit_sha", field=models.CharField(max_length=64, blank=True)),
    ]
