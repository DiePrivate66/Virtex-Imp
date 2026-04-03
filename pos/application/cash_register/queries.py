from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from pos.models import AccountingAdjustment, CajaTurno, Cliente, MovimientoCaja, Venta


@dataclass(frozen=True)
class CashTurnMetrics:
    total_efectivo: Decimal
    total_transferencia: Decimal
    total_tarjeta: Decimal
    total_ingresos: Decimal
    total_egresos: Decimal
    total_reembolsos_heredados_turno: Decimal
    refund_adjustments_open: object
    refund_adjustments_open_count: int
    refund_adjustments_open_total: Decimal
    movimientos: object


def calculate_cash_turn_metrics(caja) -> CashTurnMetrics:
    if not caja:
        empty_queryset = AccountingAdjustment.objects.none()
        empty_movimientos = MovimientoCaja.objects.none()
        return CashTurnMetrics(
            total_efectivo=Decimal('0.00'),
            total_transferencia=Decimal('0.00'),
            total_tarjeta=Decimal('0.00'),
            total_ingresos=Decimal('0.00'),
            total_egresos=Decimal('0.00'),
            total_reembolsos_heredados_turno=Decimal('0.00'),
            refund_adjustments_open=empty_queryset,
            refund_adjustments_open_count=0,
            refund_adjustments_open_total=Decimal('0.00'),
            movimientos=empty_movimientos,
        )

    ventas_turno = Venta.objects.filter(turno=caja, payment_status=Venta.PaymentStatus.PAID).exclude(estado='CANCELADO')
    total_efectivo = ventas_turno.filter(metodo_pago='EFECTIVO').aggregate(total=Sum('total'))['total'] or Decimal('0.00')
    total_transferencia = (
        ventas_turno.filter(metodo_pago='TRANSFERENCIA').aggregate(total=Sum('total'))['total'] or Decimal('0.00')
    )
    total_tarjeta = ventas_turno.filter(metodo_pago='TARJETA').aggregate(total=Sum('total'))['total'] or Decimal('0.00')

    movimientos = MovimientoCaja.objects.filter(turno=caja).exclude(concepto='VENTA')
    total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0.00')
    total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or Decimal('0.00')
    total_reembolsos_heredados_turno = (
        movimientos.filter(
            tipo='EGRESO',
            concepto=MovimientoCaja.CONCEPTO_REEMBOLSO_HEREDADO,
        ).aggregate(total=Sum('monto'))['total']
        or Decimal('0.00')
    )
    refund_adjustments_open = get_open_refund_adjustments_for_cash_register(caja)
    refund_adjustments_open_total = refund_adjustments_open.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    return CashTurnMetrics(
        total_efectivo=total_efectivo,
        total_transferencia=total_transferencia,
        total_tarjeta=total_tarjeta,
        total_ingresos=total_ingresos,
        total_egresos=total_egresos,
        total_reembolsos_heredados_turno=total_reembolsos_heredados_turno,
        refund_adjustments_open=refund_adjustments_open,
        refund_adjustments_open_count=refund_adjustments_open.count(),
        refund_adjustments_open_total=refund_adjustments_open_total,
        movimientos=movimientos,
    )


def get_open_cash_register_for_user(user):
    return CajaTurno.objects.filter(usuario=user, fecha_cierre__isnull=True).first()


def get_locked_open_cash_register_for_user(user):
    return CajaTurno.objects.select_for_update().filter(usuario=user, fecha_cierre__isnull=True).first()


def get_cash_opening_context(user) -> dict:
    return {'caja_abierta': get_open_cash_register_for_user(user)}


def get_open_refund_adjustments_for_cash_register(caja):
    if not caja:
        return AccountingAdjustment.objects.none()

    queryset = AccountingAdjustment.objects.filter(
        organization=caja.organization,
        account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY,
        status=AccountingAdjustment.Status.OPEN,
    ).select_related('sale', 'source_audit_log', 'created_by', 'source_account', 'destination_account')

    if caja.location_id:
        queryset = queryset.filter(location_id=caja.location_id)
    else:
        queryset = queryset.filter(location__isnull=True)

    return queryset.order_by('-effective_at', '-created_at')


def get_cash_available_on_turn(caja) -> Decimal:
    if not caja:
        return Decimal('0.00')

    metrics = calculate_cash_turn_metrics(caja)
    return caja.base_inicial + metrics.total_efectivo + metrics.total_ingresos - metrics.total_egresos


def get_cash_closing_context(user):
    caja = get_open_cash_register_for_user(user)
    if not caja:
        return None

    metrics = calculate_cash_turn_metrics(caja)

    caja.total_efectivo_sistema = metrics.total_efectivo
    caja.total_transferencia_sistema = metrics.total_transferencia
    caja.total_otros_sistema = metrics.total_tarjeta
    caja.save()

    return {
        'caja': caja,
        'total_ingresos_caja': metrics.total_ingresos,
        'total_egresos_caja': metrics.total_egresos,
        'total_reembolsos_heredados_turno': metrics.total_reembolsos_heredados_turno,
        'movimientos_caja': metrics.movimientos,
        'refund_adjustments_open': metrics.refund_adjustments_open[:10],
        'refund_adjustments_open_count': metrics.refund_adjustments_open_count,
        'refund_adjustments_open_total': metrics.refund_adjustments_open_total,
    }


def find_customer_by_identity_document(cedula: str):
    return Cliente.objects.filter(cedula_ruc=cedula).first()
