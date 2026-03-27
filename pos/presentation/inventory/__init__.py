"""Presentation layer for inventory views."""

from .views import (
    api_actualizar_minimo,
    api_movimiento_inventario,
    historial_inventario,
    panel_inventario,
    reporte_inventario_pdf,
)

__all__ = [
    'api_actualizar_minimo',
    'api_movimiento_inventario',
    'historial_inventario',
    'panel_inventario',
    'reporte_inventario_pdf',
]
