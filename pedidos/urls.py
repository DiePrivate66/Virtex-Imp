"""Compatibility facade for public ordering URLs.

Historical module: ``pedidos.urls``
Canonical target: ``pos.presentation.api.urls``
Retirement phase: ``phase_4_retire_legacy_entrypoints``
"""

from pos.legacy import build_legacy_module_metadata, warn_legacy_wrapper_import

(
    LEGACY_MODULE_PATH,
    LEGACY_CONTRACT,
    CANONICAL_TARGET,
    COMPATIBILITY_ROLE,
    REMOVAL_PHASE,
) = build_legacy_module_metadata('pedidos.urls')

warn_legacy_wrapper_import(LEGACY_MODULE_PATH)

from pos.presentation.api.urls import urlpatterns

__all__ = [
    'CANONICAL_TARGET',
    'COMPATIBILITY_ROLE',
    'LEGACY_CONTRACT',
    'LEGACY_MODULE_PATH',
    'REMOVAL_PHASE',
    'urlpatterns',
]
