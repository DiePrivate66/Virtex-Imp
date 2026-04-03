from __future__ import annotations

from django.db.models import F

from pos.application.context import resolve_catalog_organization_for_user
from pos.models import Inventario, MovimientoInventario, Producto

from .commands import ensure_inventory_for_product


def get_inventory_panel_context(user) -> dict:
    organization = resolve_catalog_organization_for_user(user)
    productos_sin_inv = Producto.objects.filter(organization=organization, inventario__isnull=True)
    for producto in productos_sin_inv:
        ensure_inventory_for_product(producto)

    inventarios = Inventario.objects.select_related('producto__categoria').filter(
        producto__organization=organization
    ).order_by(
        'producto__categoria__nombre',
        'producto__nombre',
    )

    return {
        'inventarios': inventarios,
        'total_productos': inventarios.count(),
        'bajo_stock': inventarios.filter(stock_actual__lte=F('stock_minimo')).count(),
        'sin_stock': inventarios.filter(stock_actual__lte=0).count(),
    }


def get_inventory_history_context(producto_id, *, user):
    organization = resolve_catalog_organization_for_user(user)
    producto = Producto.objects.get(id=producto_id, organization=organization)
    inventario = ensure_inventory_for_product(producto)
    movimientos = MovimientoInventario.objects.filter(producto=producto)[:100]
    return {
        'producto': producto,
        'inventario': inventario,
        'movimientos': movimientos,
    }


def get_inventory_report_context(*, ahora, usuario, user):
    organization = resolve_catalog_organization_for_user(user)
    inventarios = Inventario.objects.select_related('producto__categoria').filter(
        producto__organization=organization
    ).order_by(
        'producto__categoria__nombre',
        'producto__nombre',
    )
    bajo_stock = inventarios.filter(stock_actual__lte=F('stock_minimo'))
    sin_stock = inventarios.filter(stock_actual__lte=0)

    return {
        'inventarios': inventarios,
        'bajo_stock': bajo_stock,
        'sin_stock': sin_stock,
        'total_productos': inventarios.count(),
        'ahora': ahora,
        'usuario': usuario,
    }
