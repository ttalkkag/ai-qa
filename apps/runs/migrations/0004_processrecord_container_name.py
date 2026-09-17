from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("runs", "0003_job_cancel_reason_job_cancel_requested_at")]
    operations = [migrations.AddField(model_name="processrecord", name="container_name",
                                     field=models.CharField(blank=True, max_length=80))]
