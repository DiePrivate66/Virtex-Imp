from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from pos.application.context import ensure_staff_profile_for_user
from pos.application.cash_register.queries import calculate_cash_turn_metrics, get_open_refund_adjustments_for_cash_register
from pos.models import (
    AuditLog,
    Asistencia,
    CajaTurno,
    Cliente,
    Empleado,
    LocationAssignment,
    StaffProfile,
)


PIN_LOCKOUT_ATTEMPTS = 3
PIN_LOCKOUT_MINUTES = 15


class CashRegisterError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class PosPinVerificationResult:
    user: object
    empleado: Empleado | None
    staff_profile: StaffProfile | None
    rol: str
    empleado_nombre: str


def _finalize_cash_register_close(*, caja: CajaTurno, staff_profile: StaffProfile, total_declarado, conteo) -> CajaTurno:
    try:
        monto_efectivo_real = Decimal(str(total_declarado or 0)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CashRegisterError('Total declarado invalido') from exc

    if monto_efectivo_real < 0:
        raise CashRegisterError('El total declarado no puede ser negativo')
    if not isinstance(conteo, dict):
        raise CashRegisterError('El conteo de billetes debe ser un objeto JSON')

    metrics = calculate_cash_turn_metrics(caja)
    esperado = caja.base_inicial + metrics.total_efectivo + metrics.total_ingresos - metrics.total_egresos

    caja.operator_closed_by = staff_profile
    caja.fecha_cierre = timezone.now()
    caja.monto_final_declarado = monto_efectivo_real
    caja.conteo_billetes = conteo
    caja.total_efectivo_sistema = metrics.total_efectivo
    caja.total_transferencia_sistema = metrics.total_transferencia
    caja.total_otros_sistema = metrics.total_tarjeta
    caja.diferencia = monto_efectivo_real - esperado
    caja.save(
        update_fields=[
            'operator_closed_by',
            'fecha_cierre',
            'monto_final_declarado',
            'conteo_billetes',
            'total_efectivo_sistema',
            'total_transferencia_sistema',
            'total_otros_sistema',
            'diferencia',
        ]
    )
    return caja


def verify_pos_pin(
    pin: str | None,
    *,
    alias: str | None = None,
    location_uuid: str | None = None,
) -> PosPinVerificationResult:
    if alias and location_uuid:
        return _verify_staff_alias_pin(pin=pin, alias=alias, location_uuid=location_uuid)
    return _verify_legacy_employee_pin(pin=pin)


def _verify_staff_alias_pin(*, pin: str | None, alias: str, location_uuid: str) -> PosPinVerificationResult:
    if not pin:
        raise CashRegisterError('PIN Incorrecto')

    alias_normalized = ' '.join(str(alias or '').strip().lower().split())
    pending_error: CashRegisterError | None = None
    success_result: PosPinVerificationResult | None = None
    with transaction.atomic():
        assignment = (
            LocationAssignment.objects.select_related(
                'location__organization',
                'staff_profile__membership__user',
            )
            .select_for_update()
            .filter(
                location__uuid=location_uuid,
                location__active=True,
                location__organization__active=True,
                alias_normalized=alias_normalized,
                active=True,
                staff_profile__active=True,
                staff_profile__membership__active=True,
            )
            .first()
        )
        if not assignment:
            raise CashRegisterError('Alias o PIN incorrecto')

        staff_profile = StaffProfile.objects.select_for_update().select_related(
            'membership__user'
        ).get(id=assignment.staff_profile_id)

        if staff_profile.pin_blocked_until and staff_profile.pin_blocked_until > timezone.now():
            pending_error = CashRegisterError(
                'PIN bloqueado temporalmente. Solicita ayuda a un supervisor.',
                status_code=423,
            )
        else:
            legacy_employee = getattr(staff_profile.user, 'empleado', None)
            if legacy_employee and legacy_employee.pin and not staff_profile.pin_hash:
                staff_profile.pin_hash = make_password(legacy_employee.pin)
                staff_profile.requires_pin_setup = False
                staff_profile.save(update_fields=['pin_hash', 'requires_pin_setup', 'updated_at'])

            if not staff_profile.pin_hash or not check_password(pin, staff_profile.pin_hash):
                staff_profile.pin_failed_attempts += 1
                update_fields = ['pin_failed_attempts', 'updated_at']
                if staff_profile.pin_failed_attempts >= PIN_LOCKOUT_ATTEMPTS:
                    staff_profile.pin_blocked_until = timezone.now() + timedelta(minutes=PIN_LOCKOUT_MINUTES)
                    update_fields.append('pin_blocked_until')
                staff_profile.save(update_fields=update_fields)
                pending_error = CashRegisterError('Alias o PIN incorrecto')
            elif staff_profile.operational_role not in [
                StaffProfile.OperationalRole.ADMIN,
                StaffProfile.OperationalRole.CAJERO,
                StaffProfile.OperationalRole.MANAGER,
            ]:
                pending_error = CashRegisterError('Rol no autorizado para POS')
            else:
                if staff_profile.pin_failed_attempts or staff_profile.pin_blocked_until:
                    staff_profile.pin_failed_attempts = 0
                    staff_profile.pin_blocked_until = None
                    staff_profile.save(update_fields=['pin_failed_attempts', 'pin_blocked_until', 'updated_at'])

                _ensure_attendance_open(legacy_employee)
                success_result = PosPinVerificationResult(
                    user=staff_profile.user,
                    empleado=legacy_employee,
                    staff_profile=staff_profile,
                    rol=staff_profile.operational_role,
                    empleado_nombre=staff_profile.display_name,
                )

    if pending_error:
        raise pending_error
    return success_result


def _verify_legacy_employee_pin(*, pin: str | None) -> PosPinVerificationResult:
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

    staff_profile = ensure_staff_profile_for_user(empleado.usuario)
    _ensure_attendance_open(empleado)

    return PosPinVerificationResult(
        user=empleado.usuario,
        empleado=empleado,
        staff_profile=staff_profile,
        rol=empleado.rol,
        empleado_nombre=(empleado.nombre or '').strip(),
    )


def _ensure_attendance_open(empleado: Empleado | None) -> None:
    if not empleado:
        return

    hoy = timezone.localtime().date()
    ya_abierta = Asistencia.objects.filter(
        empleado=empleado,
        fecha=hoy,
        hora_salida__isnull=True,
    ).exists()
    if not ya_abierta:
        Asistencia.objects.create(empleado=empleado)


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

    staff_profile = ensure_staff_profile_for_user(user)
    caja = CajaTurno.objects.create(
        usuario=user,
        base_inicial=monto,
        organization=staff_profile.organization,
        location=staff_profile.assignments.filter(active=True).order_by('id').first().location
        if staff_profile.assignments.filter(active=True).exists()
        else None,
        operator_opened_by=staff_profile,
    )
    return caja, False


@transaction.atomic
def close_cash_register(
    user,
    total_declarado,
    conteo,
    *,
    allow_pending_refund_override: bool = False,
    pending_refund_override_note: str = '',
):
    caja = CajaTurno.objects.select_for_update().filter(usuario=user, fecha_cierre__isnull=True).first()
    if not caja:
        raise CashRegisterError('No existe una caja abierta', status_code=404)

    refund_adjustments_open = get_open_refund_adjustments_for_cash_register(caja)
    refund_adjustments_open_count = refund_adjustments_open.count()
    if refund_adjustments_open_count:
        refund_adjustments_open_total = (
            refund_adjustments_open.aggregate(total=Sum('amount'))['total'] or 0
        )
        override_note = (pending_refund_override_note or '').strip()
        if not allow_pending_refund_override:
            raise CashRegisterError(
                (
                    'No se puede cerrar caja mientras existan '
                    f'{refund_adjustments_open_count} reembolso(s) pendiente(s) '
                    f'por ${refund_adjustments_open_total:.2f}. '
                    'Resuelve los ajustes REFUND_REQUIRED o registra un cierre con deuda pendiente.'
                ),
                status_code=409,
            )
        if not override_note:
            raise CashRegisterError(
                'Debes registrar una justificacion para cerrar caja con reembolsos pendientes.',
                status_code=400,
            )

        AuditLog.objects.create(
            organization=caja.organization,
            location=caja.location,
            actor_user=user,
            event_type='cash_register.closed_with_pending_refunds',
            target_model='CajaTurno',
            target_id=str(caja.id),
            payload_json={
                'pending_refund_adjustment_ids': list(refund_adjustments_open.values_list('id', flat=True)[:25]),
                'pending_refund_count': refund_adjustments_open_count,
                'pending_refund_total': f'{refund_adjustments_open_total:.2f}',
                'override_note': override_note[:255],
            },
        )

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

    staff_profile = ensure_staff_profile_for_user(user)
    return _finalize_cash_register_close(
        caja=caja,
        staff_profile=staff_profile,
        total_declarado=total_declarado,
        conteo=conteo,
    )


def upsert_customer(data: dict, *, organization) -> Cliente:
    cedula = data.get('cedula')
    if not is_valid_identity_document(cedula):
        raise CashRegisterError('C.I/RUC invalido (10 o 13 digitos)')

    cliente, created = Cliente.objects.get_or_create(
        organization=organization,
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
        cliente.save(update_fields=['nombre', 'direccion', 'telefono', 'email'])

    return cliente


def is_valid_identity_document(value: str | None) -> bool:
    return bool(value) and value.isdigit() and len(value) in (10, 13)
