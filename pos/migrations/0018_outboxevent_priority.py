from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0017_auditlog_attention_fields'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='outboxevent',
            options={'ordering': ['priority', 'created_at']},
        ),
        migrations.AddField(
            model_name='outboxevent',
            name='priority',
            field=models.PositiveSmallIntegerField(
                choices=[(10, 'Critical'), (20, 'High'), (30, 'Normal'), (40, 'Low')],
                db_index=True,
                default=30,
            ),
        ),
        migrations.RemoveIndex(
            model_name='outboxevent',
            name='pos_outboxe_status_b5a855_idx',
        ),
        migrations.AddIndex(
            model_name='outboxevent',
            index=models.Index(fields=['status', 'priority', 'available_at'], name='pos_outboxe_status_0561aa_idx'),
        ),
    ]
