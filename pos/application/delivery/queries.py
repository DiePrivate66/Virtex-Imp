from __future__ import annotations

from pos.infrastructure.delivery import (
    read_delivery_claim_token,
    read_delivery_delivered_token,
    read_delivery_in_transit_token,
    read_delivery_quote_token,
)
from pos.models import DeliveryQuote, Venta

from .commands import DeliveryError


def get_manual_delivery_portal_context() -> dict:
    pedidos = Venta.objects.filter(estado='PENDIENTE_COTIZACION').order_by('-fecha')
    return {'pedidos': pedidos}


def get_delivery_quote_form_context(token: str) -> dict:
    try:
        payload = read_delivery_quote_token(token)
    except Exception as exc:
        raise DeliveryError('Token invalido o expirado') from exc

    venta = Venta.objects.filter(id=payload['venta_id']).first()
    if not venta:
        raise DeliveryError('Pedido no encontrado', status_code=404)

    ya_usado = DeliveryQuote.objects.filter(
        venta_id=payload['venta_id'],
        empleado_delivery_id=payload['empleado_id'],
    ).exists()

    return {
        'token': token,
        'venta': venta,
        'ya_cotizado': venta.estado != 'PENDIENTE_COTIZACION' or ya_usado,
    }


def get_delivery_claim_form_context(token: str) -> dict:
    try:
        payload = read_delivery_claim_token(token)
    except Exception:
        return {'token_invalido': True, 'venta': None}

    venta = Venta.objects.prefetch_related('detalles__producto').filter(id=payload['venta_id']).first()
    if not venta:
        raise DeliveryError('Pedido no encontrado', status_code=404)

    ya_tomado = venta.repartidor_asignado is not None or venta.estado != 'PENDIENTE_COTIZACION'
    return {
        'token': token,
        'venta': venta,
        'ya_tomado': ya_tomado,
        'token_invalido': False,
    }


def get_delivery_in_transit_form_context(token: str) -> dict:
    try:
        payload = read_delivery_in_transit_token(token)
    except Exception:
        return {'token_invalido': True, 'venta': None}

    venta = Venta.objects.prefetch_related('detalles__producto').select_related('repartidor_asignado').filter(id=payload['venta_id']).first()
    if not venta:
        raise DeliveryError('Pedido no encontrado', status_code=404)

    link_invalido = venta.repartidor_asignado_id != payload['empleado_id']
    ya_en_camino = venta.estado == 'EN_CAMINO'
    return {
        'token': token,
        'venta': venta,
        'ya_en_camino': ya_en_camino,
        'link_invalido': link_invalido,
        'token_invalido': False,
    }


def get_delivery_delivered_form_context(token: str) -> dict:
    try:
        payload = read_delivery_delivered_token(token)
    except Exception:
        return {'token_invalido': True, 'venta': None}

    venta = (
        Venta.objects.prefetch_related('detalles__producto')
        .select_related('repartidor_asignado')
        .filter(id=payload['venta_id'])
        .first()
    )
    if not venta:
        raise DeliveryError('Pedido no encontrado', status_code=404)

    link_invalido = venta.repartidor_asignado_id != payload['empleado_id']
    entrega_confirmada = venta.repartidor_confirmo_entrega_at is not None or venta.estado == 'LISTO'
    esperando_cliente = venta.cliente_reporto_recibido_at is None
    return {
        'token': token,
        'venta': venta,
        'entrega_confirmada': entrega_confirmada,
        'esperando_cliente': esperando_cliente,
        'link_invalido': link_invalido,
        'token_invalido': False,
    }
