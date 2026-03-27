"""Compatibility facade for public API views.

Historical internal module: ``pos.presentation.api.views``
Canonical target: ``pos.presentation.api.public``
"""

from .public import (
    api_crear_pedido,
    api_productos,
    confirmacion_pedido,
    esta_abierto,
    menu_cliente,
)

__all__ = [
    'api_crear_pedido',
    'api_productos',
    'confirmacion_pedido',
    'esta_abierto',
    'menu_cliente',
]
