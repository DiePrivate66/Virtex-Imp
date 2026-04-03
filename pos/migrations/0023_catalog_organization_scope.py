from django.db import migrations, models
import django.db.models.deletion


def backfill_catalog_organization(apps, schema_editor):
    Organization = apps.get_model('pos', 'Organization')
    Categoria = apps.get_model('pos', 'Categoria')
    Producto = apps.get_model('pos', 'Producto')

    default_org, _created = Organization.objects.get_or_create(
        slug='legacy-default',
        defaults={
            'name': 'Legacy Default Organization',
            'active': True,
        },
    )

    Categoria.objects.filter(organization__isnull=True).update(organization=default_org)

    for producto in Producto.objects.select_related('categoria').filter(organization__isnull=True):
        producto.organization_id = producto.categoria.organization_id or default_org.id
        producto.save(update_fields=['organization'])


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0022_ledgerregistryactivation'),
    ]

    operations = [
        migrations.AddField(
            model_name='categoria',
            name='organization',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='categories',
                to='pos.organization',
            ),
        ),
        migrations.AddField(
            model_name='producto',
            name='organization',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='products',
                to='pos.organization',
            ),
        ),
        migrations.RunPython(backfill_catalog_organization, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='categoria',
            name='organization',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='categories',
                to='pos.organization',
            ),
        ),
        migrations.AlterField(
            model_name='producto',
            name='organization',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='products',
                to='pos.organization',
            ),
        ),
    ]
