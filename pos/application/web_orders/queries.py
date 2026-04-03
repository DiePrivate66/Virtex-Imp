from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from pos.application.context import get_default_catalog_organization
from pos.domain.web_orders import (
    STATUS_CANCELLED,
    STATUS_PENDING_QUOTE,
    STATUS_READY,
)
from pos.models import Categoria, Producto, Venta


def store_is_open() -> bool:
    if not getattr(settings, 'ENABLE_BUSINESS_HOURS', True):
        return True

    now = timezone.localtime()
    weekday = now.weekday()
    hour_decimal = now.hour + (now.minute / 60)

    if weekday == 6:
        return 16.0 <= hour_decimal < 21.0
    return 16.0 <= hour_decimal < 22.0


def get_menu_page_context() -> dict:
    now = timezone.localtime()
    weekday = now.weekday()
    organization = get_default_catalog_organization()

    return {
        'categorias': Categoria.objects.filter(organization=organization),
        'productos': Producto.objects.filter(organization=organization, activo=True).select_related('categoria'),
        'local_abierto': store_is_open(),
        'horario_hoy': '4:00 PM - 9:00 PM' if weekday == 6 else '4:00 PM - 10:00 PM',
        'dia_hoy': ['Lunes', 'Martes', 'Miercoles', 'Jueves', 'Viernes', 'Sabado', 'Domingo'][weekday],
    }


def build_product_catalog_payload() -> dict:
    organization = get_default_catalog_organization()
    categories_payload = []
    for category in Categoria.objects.filter(organization=organization):
        products = Producto.objects.filter(organization=organization, categoria=category, activo=True)
        categories_payload.append(
            {
                'id': category.id,
                'nombre': category.nombre,
                'productos': [
                    {
                        'id': product.id,
                        'nombre': product.nombre,
                        'precio': str(product.precio),
                    }
                    for product in products
                ],
            }
        )

    return {'categorias': categories_payload}


def get_closed_store_message() -> str:
    now = timezone.localtime()
    weekday = now.weekday()
    schedule = '4:00 PM - 9:00 PM' if weekday == 6 else '4:00 PM - 10:00 PM'
    return f'Lo sentimos, estamos cerrados. Nuestro horario es: {schedule}'


def timed_out_quote_count() -> int:
    return Venta.objects.filter(
        origen='WEB',
        estado=STATUS_PENDING_QUOTE,
        delivery_quote_deadline_at__isnull=False,
        delivery_quote_deadline_at__lt=timezone.now(),
    ).count()


def get_web_orders_panel_context(limit: int = 50):
    return {
        'pedidos': (
            Venta.objects.filter(origen='WEB')
            .exclude(estado=STATUS_CANCELLED)
            .select_related('repartidor_asignado')
            .order_by('-fecha')[:limit]
        ),
        'timed_out_quote_count': timed_out_quote_count(),
    }


def build_web_orders_payload(limit: int = 50):
    pedidos = (
        Venta.objects.filter(origen='WEB')
        .exclude(estado__in=[STATUS_CANCELLED, STATUS_READY])
        .prefetch_related('detalles__producto')
        .order_by('-fecha')[:limit]
    )
    data = []
    for pedido in pedidos:
        items = [
            {
                'nombre': detalle.producto.nombre,
                'cantidad': detalle.cantidad,
                'nota': detalle.nota,
                'subtotal': str(detalle.subtotal),
            }
            for detalle in pedido.detalles.all()
        ]
        data.append(
            {
                'id': pedido.id,
                'estado': pedido.estado,
                'estado_display': pedido.get_estado_display(),
                'estado_pago': pedido.estado_pago,
                'cliente_nombre': pedido.cliente_nombre,
                'telefono': pedido.telefono_cliente,
                'direccion': pedido.direccion_envio,
                'tipo_pedido': pedido.tipo_pedido,
                'tipo_pedido_display': pedido.get_tipo_pedido_display(),
                'metodo_pago': pedido.metodo_pago,
                'metodo_pago_display': pedido.get_metodo_pago_display(),
                'referencia_pago': pedido.referencia_pago,
                'tarjeta_tipo': pedido.tarjeta_tipo,
                'tarjeta_marca': pedido.tarjeta_marca,
                'total': str(pedido.total),
                'costo_envio': str(pedido.costo_envio),
                'comprobante': pedido.comprobante_foto.url if pedido.comprobante_foto else None,
                'fecha': pedido.fecha.strftime('%H:%M'),
                'items': items,
            }
        )
    return {
        'pedidos': data,
        'count': len(data),
        'timed_out_quote_count': timed_out_quote_count(),
    }
