from __future__ import annotations

import json

from pos.application.web_orders import WebOrderError


def parse_web_order_request(request) -> tuple[dict, object | None]:
    if request.content_type and 'multipart/form-data' in request.content_type:
        return _parse_multipart_web_order_request(request)
    return _parse_json_web_order_request(request)


def _parse_multipart_web_order_request(request) -> tuple[dict, object | None]:
    data = {
        'nombre': request.POST.get('nombre', 'CONSUMIDOR FINAL'),
        'cedula': request.POST.get('cedula', ''),
        'telefono': request.POST.get('telefono', ''),
        'direccion': request.POST.get('direccion', ''),
        'tipo_pedido': request.POST.get('tipo_pedido', 'DOMICILIO'),
        'metodo_pago': request.POST.get('metodo_pago', 'EFECTIVO'),
        'carrito': _parse_cart_payload(request.POST.get('carrito', '[]')),
        'ubicacion_lat': request.POST.get('ubicacion_lat'),
        'ubicacion_lng': request.POST.get('ubicacion_lng'),
    }
    return data, request.FILES.get('comprobante')


def _parse_json_web_order_request(request) -> tuple[dict, None]:
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError as exc:
        raise WebOrderError('Payload JSON invalido', status_code=400) from exc

    if not isinstance(data, dict):
        raise WebOrderError('Payload JSON invalido', status_code=400)

    return data, None


def _parse_cart_payload(raw_cart: str):
    try:
        cart = json.loads(raw_cart)
    except json.JSONDecodeError as exc:
        raise WebOrderError('Carrito invalido', status_code=400) from exc

    if not isinstance(cart, list):
        raise WebOrderError('Carrito invalido', status_code=400)

    return cart
