from django.db import migrations


LEGACY_TO_V2_PAYMENT_STATUS = {
    'PENDIENTE': 'PENDING',
    'APROBADO': 'PAID',
    'RECHAZADO': 'FAILED',
    'ANULADO': 'VOIDED',
}

V2_TO_LEGACY_PAYMENT_STATUS = {
    'PENDING': 'PENDIENTE',
    'PAID': 'APROBADO',
    'FAILED': 'RECHAZADO',
    'VOIDED': 'ANULADO',
}


def backfill_sale_payment_fields(apps, schema_editor):
    Venta = apps.get_model('pos', 'Venta')
    db_alias = schema_editor.connection.alias

    queryset = Venta.objects.using(db_alias).all().only(
        'id',
        'payment_status',
        'estado_pago',
        'payment_method_type',
        'metodo_pago',
        'payment_reference',
        'referencia_pago',
    )

    for venta in queryset.iterator():
        resolved_payment_status = (venta.payment_status or '').strip().upper()
        if not resolved_payment_status:
            resolved_payment_status = LEGACY_TO_V2_PAYMENT_STATUS.get(
                (venta.estado_pago or '').strip().upper(),
                'PAID',
            )

        resolved_payment_method_type = (venta.payment_method_type or venta.metodo_pago or '').strip().upper()
        resolved_metodo_pago = (venta.metodo_pago or resolved_payment_method_type or '').strip().upper()
        resolved_payment_reference = (venta.payment_reference or venta.referencia_pago or '').strip()
        resolved_estado_pago = V2_TO_LEGACY_PAYMENT_STATUS.get(resolved_payment_status, venta.estado_pago or 'APROBADO')

        update_fields = {}
        if venta.payment_status != resolved_payment_status:
            update_fields['payment_status'] = resolved_payment_status
        if venta.payment_method_type != resolved_payment_method_type:
            update_fields['payment_method_type'] = resolved_payment_method_type
        if venta.metodo_pago != resolved_metodo_pago:
            update_fields['metodo_pago'] = resolved_metodo_pago
        if venta.payment_reference != resolved_payment_reference[:80]:
            update_fields['payment_reference'] = resolved_payment_reference[:80]
        if venta.referencia_pago != resolved_payment_reference[:40]:
            update_fields['referencia_pago'] = resolved_payment_reference[:40]
        if venta.estado_pago != resolved_estado_pago:
            update_fields['estado_pago'] = resolved_estado_pago

        if update_fields:
            Venta.objects.using(db_alias).filter(id=venta.id).update(**update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0024_cliente_organization_scope'),
    ]

    operations = [
        migrations.RunPython(backfill_sale_payment_fields, migrations.RunPython.noop),
    ]
