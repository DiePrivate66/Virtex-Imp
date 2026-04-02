from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0019_accountingadjustment'),
    ]

    operations = [
        migrations.AddField(
            model_name='accountingadjustment',
            name='destination_account',
            field=models.CharField(
                choices=[
                    ('PAYMENT_GATEWAY_CLEARING', 'Cobros pasarela / banco'),
                    ('UNIDENTIFIED_RECEIPTS', 'Ingresos por identificar'),
                    ('REFUND_PAYABLE', 'Reembolsos pendientes'),
                ],
                default='UNIDENTIFIED_RECEIPTS',
                max_length=40,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='accountingadjustment',
            name='source_account',
            field=models.CharField(
                choices=[
                    ('PAYMENT_GATEWAY_CLEARING', 'Cobros pasarela / banco'),
                    ('UNIDENTIFIED_RECEIPTS', 'Ingresos por identificar'),
                    ('REFUND_PAYABLE', 'Reembolsos pendientes'),
                ],
                default='PAYMENT_GATEWAY_CLEARING',
                max_length=40,
            ),
            preserve_default=False,
        ),
    ]
