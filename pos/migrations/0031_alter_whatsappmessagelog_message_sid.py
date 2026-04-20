from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0030_alter_venta_metodo_pago'),
    ]

    operations = [
        migrations.AlterField(
            model_name='whatsappmessagelog',
            name='message_sid',
            field=models.CharField(blank=True, max_length=255, null=True, unique=True),
        ),
    ]
