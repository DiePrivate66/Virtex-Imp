from __future__ import annotations

from django.db.models import F

from pos.models import Inventario, MovimientoInventario, Producto

from .commands import ensure_inventory_for_product


def get_inventory_panel_context() -> dict:
    productos_sin_inv = Producto.objects.filter(inventario__isnull=True)
    for producto in productos_sin_inv:
        ensure_inventory_for_product(producto)

    inventarios = Inventario.objects.select_related('producto__categoria').all().order_by(
        'producto__categoria__nombre',
        'producto__nombre',
    )

    return {
        'inventarios': inventarios,
        'total_productos': inventarios.count(),
        'bajo_stock': inventarios.filter(stock_actual__lte=F('stock_minimo')).count(),
        'sin_stock': inventarios.filter(stock_actual__lte=0).count(),
    }


def get_inventory_history_context(producto_id):
    producto = Producto.objects.get(id=producto_id)
    inventario = ensure_inventory_for_product(producto)
    movimientos = MovimientoInventario.objects.filter(producto=producto)[:100]
    return {
        'producto': producto,
        'inventario': inventario,
        'movimientos': movimientos,
    }


def get_inventory_report_context(*, ahora, usuario) -> dict:
    inventarios = Inventario.objects.select_related('producto__categoria').all().order_by(
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
