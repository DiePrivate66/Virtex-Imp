from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0013_venta_salio_a_reparto_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='venta',
            name='cliente_reporto_recibido_at',
            field=models.DateTimeField(
                blank=True,
                help_text='Momento en que el cliente reporto haber recibido el pedido.',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='venta',
            name='email_cliente',
            field=models.EmailField(
                blank=True,
                help_text='Correo del cliente para enviar comprobante final',
                max_length=254,
            ),
        ),
        migrations.AddField(
            model_name='venta',
            name='repartidor_confirmo_entrega_at',
            field=models.DateTimeField(
                blank=True,
                help_text='Momento en que el repartidor confirmo la entrega final.',
                null=True,
            ),
        ),
    ]
