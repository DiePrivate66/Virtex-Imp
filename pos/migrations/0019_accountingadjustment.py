import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0018_outboxevent_priority'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AccountingAdjustment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('adjustment_type', models.CharField(choices=[('ORPHAN_PAYMENT_UNIDENTIFIED', 'Pago huerfano por identificar'), ('ORPHAN_PAYMENT_REFUND_PENDING', 'Pago huerfano con reembolso pendiente')], max_length=40)),
                ('account_bucket', models.CharField(choices=[('PENDING_IDENTIFICATION', 'Pendientes por identificar'), ('REFUND_LIABILITY', 'Reembolsos pendientes')], max_length=40)),
                ('status', models.CharField(choices=[('OPEN', 'Open'), ('RESOLVED', 'Resolved')], default='OPEN', max_length=16)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('operating_day', models.DateField(blank=True, db_index=True, null=True)),
                ('effective_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('payment_reference', models.CharField(blank=True, max_length=80)),
                ('payment_provider', models.CharField(blank=True, max_length=50)),
                ('external_reference', models.CharField(blank=True, max_length=80)),
                ('note', models.CharField(blank=True, max_length=255)),
                ('correlation_id', models.CharField(blank=True, db_index=True, max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_accounting_adjustments', to=settings.AUTH_USER_MODEL)),
                ('location', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='accounting_adjustments', to='pos.location')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='accounting_adjustments', to='pos.organization')),
                ('sale', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='accounting_adjustments', to='pos.venta')),
                ('source_audit_log', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='accounting_adjustment', to='pos.auditlog')),
            ],
            options={
                'ordering': ['-effective_at', '-created_at'],
            },
        ),
    ]
