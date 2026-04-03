from __future__ import annotations

from django.core.exceptions import ValidationError
from django.utils import timezone


def build_sale_scope_fields(
    *,
    turno=None,
    location=None,
    organization=None,
    operating_day=None,
    timestamp=None,
    fallback_to_default_location: bool = True,
    default_location_getter=None,
    compute_operating_day_fn=None,
) -> dict:
    resolved_location = location
    if not resolved_location and turno and getattr(turno, 'location_id', None):
        resolved_location = turno.location
    if not resolved_location and fallback_to_default_location and default_location_getter:
        resolved_location = default_location_getter()

    resolved_organization = organization
    if not resolved_organization and turno and getattr(turno, 'organization_id', None):
        resolved_organization = turno.organization
    if not resolved_organization and resolved_location:
        resolved_organization = resolved_location.organization

    if resolved_location and resolved_organization and resolved_location.organization_id != resolved_organization.id:
        raise ValidationError('La venta no puede pertenecer a una organizacion distinta a la de la sucursal.')
    if turno and resolved_location and getattr(turno, 'location_id', None) and turno.location_id != resolved_location.id:
        raise ValidationError('La venta no puede apuntar a una sucursal distinta al turno.')
    if turno and resolved_organization and getattr(turno, 'organization_id', None) and turno.organization_id != resolved_organization.id:
        raise ValidationError('La venta no puede apuntar a una organizacion distinta al turno.')

    resolved_operating_day = operating_day
    if not resolved_operating_day and turno and getattr(turno, 'operating_day', None):
        resolved_operating_day = turno.operating_day
    if not resolved_operating_day and resolved_location and compute_operating_day_fn:
        resolved_operating_day = compute_operating_day_fn(
            timestamp=timestamp or timezone.now(),
            timezone_name=resolved_location.timezone,
            operating_day_ends_at=resolved_location.operating_day_ends_at,
        )

    return {
        'location': resolved_location,
        'organization': resolved_organization,
        'operating_day': resolved_operating_day,
    }


def build_sale_payment_fields(
    *,
    payment_status: str = '',
    estado_pago: str = '',
    payment_method_type: str = '',
    metodo_pago: str = '',
    payment_reference: str = '',
    referencia_pago: str = '',
    valid_payment_statuses=(),
    payment_methods=(),
    legacy_to_v2_map=None,
    v2_to_legacy_map=None,
    default_payment_status: str = '',
) -> dict:
    legacy_to_v2_map = legacy_to_v2_map or {}
    v2_to_legacy_map = v2_to_legacy_map or {}
    payment_methods = dict(payment_methods)

    resolved_payment_status = str(payment_status or '').strip().upper()
    if not resolved_payment_status:
        legacy_status = str(estado_pago or '').strip().upper()
        resolved_payment_status = legacy_to_v2_map.get(legacy_status, default_payment_status)
    if resolved_payment_status not in valid_payment_statuses:
        raise ValidationError('payment_status invalido para la venta.')

    resolved_payment_method_type = str(payment_method_type or metodo_pago or '').strip().upper()
    resolved_metodo_pago = str(metodo_pago or resolved_payment_method_type or '').strip().upper()
    if resolved_payment_method_type and resolved_payment_method_type in payment_methods:
        resolved_metodo_pago = resolved_payment_method_type

    resolved_payment_reference = str(payment_reference or referencia_pago or '').strip()

    return {
        'payment_status': resolved_payment_status,
        'estado_pago': v2_to_legacy_map.get(resolved_payment_status, estado_pago),
        'payment_method_type': resolved_payment_method_type,
        'metodo_pago': resolved_metodo_pago,
        'payment_reference': resolved_payment_reference[:80],
        'referencia_pago': resolved_payment_reference[:40],
    }


def build_sale_actor_snapshot_fields(
    *,
    operator=None,
    supervisor=None,
    operator_display_name_snapshot: str = '',
    supervisor_display_name_snapshot: str = '',
) -> dict:
    return {
        'operator_display_name_snapshot': operator_display_name_snapshot or (operator.display_name if operator else ''),
        'supervisor_display_name_snapshot': supervisor_display_name_snapshot or (
            supervisor.display_name if supervisor else ''
        ),
    }
