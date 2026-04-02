from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction

from pos.application.cash_register.queries import get_locked_open_cash_register_for_user
from pos.models import MovimientoCaja


class CashMovementError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class CashMovementResult:
    id: int
    tipo: str
    monto: Decimal


def get_open_turn_for_user(user):
    from pos.models import CajaTurno

    return CajaTurno.objects.filter(usuario=user, fecha_cierre__isnull=True).first()


@transaction.atomic
def register_cash_movement(
    *,
    user,
    tipo: str = "EGRESO",
    concepto: str = "",
    descripcion: str = "",
    monto_raw=0,
) -> CashMovementResult:
    turno = get_locked_open_cash_register_for_user(user)
    if not turno:
        raise CashMovementError("No hay turno abierto")

    try:
        monto = Decimal(str(monto_raw or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CashMovementError("Monto invalido") from exc

    if monto <= 0:
        raise CashMovementError("El monto debe ser mayor a 0")
    if not concepto:
        raise CashMovementError("Selecciona un concepto")
    if tipo not in {"INGRESO", "EGRESO"}:
        raise CashMovementError("Tipo de movimiento invalido")

    movimiento = MovimientoCaja.objects.create(
        turno=turno,
        tipo=tipo,
        concepto=concepto,
        descripcion=descripcion or "",
        monto=monto,
        registrado_por=user,
    )
    return CashMovementResult(id=movimiento.id, tipo=movimiento.tipo, monto=movimiento.monto)


def delete_cash_movement(*, movimiento_id) -> None:
    try:
        movimiento = MovimientoCaja.objects.get(id=movimiento_id)
    except MovimientoCaja.DoesNotExist as exc:
        raise CashMovementError("Movimiento no encontrado", status_code=404) from exc

    movimiento.delete()
