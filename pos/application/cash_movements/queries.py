from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from pos.models import AccountingAdjustment, CajaTurno, MovimientoCaja, Venta

from .commands import get_open_turn_for_user


def get_cash_movements_panel_context(user):
    turno = get_open_turn_for_user(user)
    if not turno:
        return None

    movimientos = MovimientoCaja.objects.filter(turno=turno).exclude(concepto="VENTA")
    total_ingresos = movimientos.filter(tipo="INGRESO").aggregate(t=Sum("monto"))["t"] or Decimal("0.00")
    total_egresos = movimientos.filter(tipo="EGRESO").aggregate(t=Sum("monto"))["t"] or Decimal("0.00")
    total_reembolsos_heredados = (
        movimientos.filter(
            tipo="EGRESO",
            concepto=MovimientoCaja.CONCEPTO_REEMBOLSO_HEREDADO,
        ).aggregate(t=Sum("monto"))["t"]
        or Decimal("0.00")
    )

    return {
        "turno": turno,
        "movimientos": movimientos,
        "total_ingresos": total_ingresos,
        "total_egresos": total_egresos,
        "total_reembolsos_heredados": total_reembolsos_heredados,
        "balance": total_ingresos - total_egresos,
        "conceptos_egreso": MovimientoCaja.CONCEPTOS_EGRESO,
        "conceptos_ingreso": MovimientoCaja.CONCEPTOS_INGRESO,
    }


def get_accounting_report_context(*, desde=None, hasta=None):
    hoy = timezone.localdate()
    desde = desde or (hoy - timedelta(days=7)).isoformat()
    hasta = hasta or hoy.isoformat()

    ventas = Venta.objects.filter(
        fecha__date__gte=desde,
        fecha__date__lte=hasta,
        payment_status=Venta.PaymentStatus.PAID,
    ).exclude(estado="CANCELADO")
    movimientos = MovimientoCaja.objects.filter(fecha__date__gte=desde, fecha__date__lte=hasta).exclude(concepto="VENTA")
    accounting_adjustments = AccountingAdjustment.objects.filter(
        effective_at__date__gte=desde,
        effective_at__date__lte=hasta,
    ).select_related('source_account', 'destination_account', 'sale')
    turnos = CajaTurno.objects.filter(
        fecha_apertura__date__gte=desde,
        fecha_apertura__date__lte=hasta,
        fecha_cierre__isnull=False,
    )

    total_efectivo = ventas.filter(metodo_pago="EFECTIVO").aggregate(t=Sum("total"))["t"] or Decimal("0")
    total_transferencia = ventas.filter(metodo_pago="TRANSFERENCIA").aggregate(t=Sum("total"))["t"] or Decimal("0")
    total_tarjeta = ventas.filter(metodo_pago="TARJETA").aggregate(t=Sum("total"))["t"] or Decimal("0")
    total_ventas = total_efectivo + total_transferencia + total_tarjeta

    total_ingresos = movimientos.filter(tipo="INGRESO").aggregate(t=Sum("monto"))["t"] or Decimal("0")
    total_egresos = movimientos.filter(tipo="EGRESO").aggregate(t=Sum("monto"))["t"] or Decimal("0")
    total_egresos_reembolsos_heredados = (
        movimientos.filter(
            tipo="EGRESO",
            concepto=MovimientoCaja.CONCEPTO_REEMBOLSO_HEREDADO,
        ).aggregate(t=Sum("monto"))["t"]
        or Decimal("0")
    )
    total_ajustes_por_identificar = (
        accounting_adjustments.filter(account_bucket=AccountingAdjustment.AccountBucket.PENDING_IDENTIFICATION)
        .aggregate(t=Sum("amount"))["t"]
        or Decimal("0")
    )
    total_ajustes_reembolso = (
        accounting_adjustments.filter(account_bucket=AccountingAdjustment.AccountBucket.REFUND_LIABILITY)
        .aggregate(t=Sum("amount"))["t"]
        or Decimal("0")
    )
    egresos_por_concepto = (
        movimientos.filter(tipo="EGRESO").values("concepto").annotate(total=Sum("monto")).order_by("-total")
    )
    total_diferencia = turnos.aggregate(t=Sum("diferencia"))["t"] or Decimal("0")

    return {
        "desde": desde,
        "hasta": hasta,
        "ventas": ventas,
        "movimientos": movimientos,
        "accounting_adjustments": accounting_adjustments.order_by("-effective_at", "-created_at"),
        "turnos": turnos,
        "total_efectivo": total_efectivo,
        "total_transferencia": total_transferencia,
        "total_tarjeta": total_tarjeta,
        "total_ventas": total_ventas,
        "total_ingresos": total_ingresos,
        "total_egresos": total_egresos,
        "total_egresos_reembolsos_heredados": total_egresos_reembolsos_heredados,
        "total_ajustes_por_identificar": total_ajustes_por_identificar,
        "total_ajustes_reembolso": total_ajustes_reembolso,
        "egresos_por_concepto": egresos_por_concepto,
        "total_diferencia": total_diferencia,
        "num_ventas": ventas.count(),
        "num_turnos": turnos.count(),
    }
