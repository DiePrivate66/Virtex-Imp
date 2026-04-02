from django.db import migrations, models
import django.utils.timezone


REGISTRY_VERSION = '2026.04.02-phase1'
REGISTRY_HASH = '6582e1c58992c2c62001c435a4f699bd50e437dea24e4083bed7b3831afac38b'
MIN_SUPPORTED_QUEUE_SCHEMA = 1


def seed_activation(apps, schema_editor):
    LedgerRegistryActivation = apps.get_model('pos', 'LedgerRegistryActivation')
    LedgerRegistryActivation.objects.using(schema_editor.connection.alias).get_or_create(
        singleton_key='default',
        defaults={
            'active_registry_version': REGISTRY_VERSION,
            'active_registry_hash': REGISTRY_HASH,
            'min_supported_queue_schema': MIN_SUPPORTED_QUEUE_SCHEMA,
            'maintenance_mode': False,
            'activated_at': django.utils.timezone.now(),
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0021_ledgeraccount_catalog'),
    ]

    operations = [
        migrations.CreateModel(
            name='LedgerRegistryActivation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('singleton_key', models.CharField(default='default', editable=False, max_length=20, unique=True)),
                ('active_registry_version', models.CharField(max_length=64)),
                ('active_registry_hash', models.CharField(max_length=64)),
                ('min_supported_queue_schema', models.PositiveIntegerField(default=1)),
                ('maintenance_mode', models.BooleanField(default=False)),
                ('activated_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Ledger registry activation',
                'verbose_name_plural': 'Ledger registry activation',
            },
        ),
        migrations.RunPython(seed_activation, migrations.RunPython.noop),
    ]
