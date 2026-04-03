from __future__ import annotations

from django.core.exceptions import ValidationError


def build_cash_movement_scope_fields(
    *,
    turno=None,
    location=None,
    organization=None,
) -> dict:
    resolved_location = location
    if not resolved_location and turno and getattr(turno, 'location_id', None):
        resolved_location = turno.location

    resolved_organization = organization
    if not resolved_organization and turno and getattr(turno, 'organization_id', None):
        resolved_organization = turno.organization
    if not resolved_organization and resolved_location:
        resolved_organization = resolved_location.organization

    if turno and resolved_location and getattr(turno, 'location_id', None) and turno.location_id != resolved_location.id:
        raise ValidationError('El movimiento de caja no puede apuntar a una sucursal distinta al turno.')
    if turno and resolved_organization and getattr(turno, 'organization_id', None) and turno.organization_id != resolved_organization.id:
        raise ValidationError('El movimiento de caja no puede pertenecer a una organizacion distinta al turno.')
    if resolved_location and resolved_organization and resolved_location.organization_id != resolved_organization.id:
        raise ValidationError('El movimiento de caja no puede pertenecer a otra organizacion.')

    return {
        'location': resolved_location,
        'organization': resolved_organization,
    }


def build_inventory_movement_scope_fields(
    *,
    producto=None,
    venta=None,
    location=None,
    organization=None,
) -> dict:
    resolved_location = location
    if not resolved_location and venta and getattr(venta, 'location_id', None):
        resolved_location = venta.location

    resolved_organization = organization
    if not resolved_organization and resolved_location:
        resolved_organization = resolved_location.organization
    if not resolved_organization and venta and getattr(venta, 'organization_id', None):
        resolved_organization = venta.organization
    if not resolved_organization and producto and getattr(producto, 'organization_id', None):
        resolved_organization = producto.organization

    if producto and resolved_organization and getattr(producto, 'organization_id', None) != resolved_organization.id:
        raise ValidationError('El movimiento de inventario no puede pertenecer a una organizacion distinta al producto.')
    if resolved_location and resolved_organization and resolved_location.organization_id != resolved_organization.id:
        raise ValidationError('El movimiento de inventario no puede pertenecer a otra organizacion.')
    if venta and resolved_location and getattr(venta, 'location_id', None) and venta.location_id != resolved_location.id:
        raise ValidationError('El movimiento de inventario no puede apuntar a una sucursal distinta a la venta.')
    if venta and resolved_organization and getattr(venta, 'organization_id', None) and venta.organization_id != resolved_organization.id:
        raise ValidationError('El movimiento de inventario no puede pertenecer a una organizacion distinta a la venta.')

    return {
        'location': resolved_location,
        'organization': resolved_organization,
    }
