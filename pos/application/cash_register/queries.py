from __future__ import annotations

from django.db.models import Sum

from pos.models import CajaTurno, Cliente, MovimientoCaja, Venta


def get_open_cash_register_for_user(user):
    return CajaTurno.objects.filter(usuario=user, fecha_cierre__isnull=True).first()


def get_cash_opening_context(user) -> dict:
    return {'caja_abierta': get_open_cash_register_for_user(user)}


def get_cash_closing_context(user):
    caja = get_open_cash_register_for_user(user)
    if not caja:
        return None

    ventas_turno = Venta.objects.filter(turno=caja)
    total_efectivo = ventas_turno.filter(metodo_pago='EFECTIVO').aggregate(total=Sum('total'))['total'] or 0
    total_transferencia = ventas_turno.filter(metodo_pago='TRANSFERENCIA').aggregate(total=Sum('total'))['total'] or 0
    total_tarjeta = ventas_turno.filter(metodo_pago='TARJETA').aggregate(total=Sum('total'))['total'] or 0

    movimientos = MovimientoCaja.objects.filter(turno=caja)
    total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or 0
    total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or 0

    caja.total_efectivo_sistema = total_efectivo
    caja.total_transferencia_sistema = total_transferencia
    caja.total_otros_sistema = total_tarjeta
    caja.save()

    return {
        'caja': caja,
        'total_ingresos_caja': total_ingresos,
        'total_egresos_caja': total_egresos,
        'movimientos_caja': movimientos,
    }


def find_customer_by_identity_document(cedula: str):
    return Cliente.objects.filter(cedula_ruc=cedula).first()
