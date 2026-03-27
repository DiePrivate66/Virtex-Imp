from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from pos.models import Asistencia, CajaTurno, Cliente, Empleado


class CashRegisterError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class PosPinVerificationResult:
    empleado: Empleado
    rol: str
    empleado_nombre: str


def verify_pos_pin(pin: str | None) -> PosPinVerificationResult:
    if not pin:
        raise CashRegisterError('PIN Incorrecto')

    try:
        empleado = Empleado.objects.get(pin=pin, activo=True)
    except Empleado.DoesNotExist as exc:
        raise CashRegisterError('PIN Incorrecto') from exc

    if empleado.rol not in ['ADMIN', 'CAJERO']:
        raise CashRegisterError('Rol no autorizado para POS')

    if not empleado.usuario:
        raise CashRegisterError('Empleado sin usuario de sistema asignado')

    hoy = timezone.localtime().date()
    ya_abierta = Asistencia.objects.filter(
        empleado=empleado,
        fecha=hoy,
        hora_salida__isnull=True,
    ).exists()
    if not ya_abierta:
        Asistencia.objects.create(empleado=empleado)

    return PosPinVerificationResult(
        empleado=empleado,
        rol=empleado.rol,
        empleado_nombre=(empleado.nombre or '').strip(),
    )


def open_cash_register(user, monto_raw) -> tuple[CajaTurno, bool]:
    caja_abierta = CajaTurno.objects.filter(usuario=user, fecha_cierre__isnull=True).first()
    if caja_abierta:
        return caja_abierta, True

    try:
        monto = Decimal(str(monto_raw or 0)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CashRegisterError('Monto inicial invalido') from exc

    if monto < 0:
        raise CashRegisterError('El monto inicial no puede ser negativo')

    caja = CajaTurno.objects.create(
        usuario=user,
        base_inicial=monto,
    )
    return caja, False


@transaction.atomic
def close_cash_register(user, total_declarado, conteo):
    caja = CajaTurno.objects.filter(usuario=user, fecha_cierre__isnull=True).first()
    if not caja:
        raise CashRegisterError('No existe una caja abierta', status_code=404)

    empleado = getattr(user, 'empleado', None)
    if empleado:
        hoy = timezone.localtime().date()
        asistencia_abierta = Asistencia.objects.filter(
            empleado=empleado,
            fecha=hoy,
            hora_salida__isnull=True,
        ).last()
        if asistencia_abierta:
            asistencia_abierta.registrar_salida()

    caja.cerrar_caja(total_declarado, conteo)
    return caja


def upsert_customer(data: dict) -> Cliente:
    cedula = data.get('cedula')
    if not is_valid_identity_document(cedula):
        raise CashRegisterError('C.I/RUC invalido (10 o 13 digitos)')

    cliente, created = Cliente.objects.get_or_create(
        cedula_ruc=cedula,
        defaults={
            'nombre': data.get('nombre'),
            'direccion': data.get('direccion', ''),
            'telefono': data.get('telefono', ''),
            'email': data.get('email', ''),
        },
    )

    if not created:
        if data.get('nombre'):
            cliente.nombre = data.get('nombre')
        if data.get('direccion'):
            cliente.direccion = data.get('direccion')
        if data.get('telefono'):
            cliente.telefono = data.get('telefono')
        if data.get('email'):
            cliente.email = data.get('email')
        cliente.save()

    return cliente


def is_valid_identity_document(value: str | None) -> bool:
    return bool(value) and value.isdigit() and len(value) in (10, 13)
