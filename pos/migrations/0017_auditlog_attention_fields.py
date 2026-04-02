import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0016_printjob_uniqueness_and_tenant_guards'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='auditlog',
            name='requires_attention',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='auditlog',
            name='resolved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='auditlog',
            name='resolved_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='resolved_audit_logs',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
