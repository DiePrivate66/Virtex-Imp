"""Compatibility facade for public ordering views.

Historical module: ``pedidos.views``
Canonical target: ``pos.presentation.api.public``
Retirement phase: ``phase_4_retire_legacy_entrypoints``
"""

from pos.legacy import build_legacy_module_metadata, warn_legacy_wrapper_import

(
    LEGACY_MODULE_PATH,
    LEGACY_CONTRACT,
    CANONICAL_TARGET,
    COMPATIBILITY_ROLE,
    REMOVAL_PHASE,
) = build_legacy_module_metadata('pedidos.views')

warn_legacy_wrapper_import(LEGACY_MODULE_PATH)

from pos.presentation.api.public import (
    api_crear_pedido,
    api_productos,
    confirmacion_pedido,
    esta_abierto,
    menu_cliente,
)

__all__ = [
    'CANONICAL_TARGET',
    'COMPATIBILITY_ROLE',
    'LEGACY_CONTRACT',
    'LEGACY_MODULE_PATH',
    'REMOVAL_PHASE',
    'api_crear_pedido',
    'api_productos',
    'confirmacion_pedido',
    'esta_abierto',
    'menu_cliente',
]
