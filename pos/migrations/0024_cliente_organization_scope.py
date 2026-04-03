from django.db import migrations, models
import django.db.models.deletion


def backfill_customer_organization(apps, schema_editor):
    Organization = apps.get_model('pos', 'Organization')
    Cliente = apps.get_model('pos', 'Cliente')
    Venta = apps.get_model('pos', 'Venta')
    db_alias = schema_editor.connection.alias

    default_org, _created = Organization.objects.using(db_alias).get_or_create(
        slug='legacy-default',
        defaults={
            'name': 'Legacy Default Organization',
            'active': True,
        },
    )

    for cliente in Cliente.objects.using(db_alias).all().order_by('id'):
        related_org_ids = list(
            Venta.objects.using(db_alias)
            .filter(cliente_id=cliente.id, organization_id__isnull=False)
            .order_by('organization_id')
            .values_list('organization_id', flat=True)
            .distinct()
        )

        if not related_org_ids:
            Cliente.objects.using(db_alias).filter(id=cliente.id).update(organization_id=default_org.id)
            continue

        primary_org_id = related_org_ids[0]
        Cliente.objects.using(db_alias).filter(id=cliente.id).update(organization_id=primary_org_id)

        for org_id in related_org_ids[1:]:
            cloned_customer = Cliente.objects.using(db_alias).create(
                organization_id=org_id,
                cedula_ruc=cliente.cedula_ruc,
                nombre=cliente.nombre,
                direccion=cliente.direccion,
                telefono=cliente.telefono,
                email=cliente.email,
                fecha_registro=cliente.fecha_registro,
            )
            Venta.objects.using(db_alias).filter(cliente_id=cliente.id, organization_id=org_id).update(
                cliente_id=cloned_customer.id
            )


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0023_catalog_organization_scope'),
    ]

    operations = [
        migrations.AddField(
            model_name='cliente',
            name='organization',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='customers',
                to='pos.organization',
            ),
        ),
        migrations.AlterField(
            model_name='cliente',
            name='cedula_ruc',
            field=models.CharField(max_length=13),
        ),
        migrations.RunPython(backfill_customer_organization, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='cliente',
            name='organization',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='customers',
                to='pos.organization',
            ),
        ),
        migrations.AlterModelOptions(
            name='cliente',
            options={'ordering': ['organization__name', 'nombre']},
        ),
        migrations.AddConstraint(
            model_name='cliente',
            constraint=models.UniqueConstraint(fields=('organization', 'cedula_ruc'), name='uq_cliente_org_cedula'),
        ),
    ]
