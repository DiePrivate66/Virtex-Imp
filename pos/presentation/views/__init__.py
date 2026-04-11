"""Server-rendered view presentation modules."""

from .pos import (
    consultar_transaccion_pendiente,
    data_deletion,
    pos_index,
    privacy_policy,
    reconciliar_pago,
    registrar_venta,
    terms_of_service,
)

__all__ = [
    'consultar_transaccion_pendiente',
    'data_deletion',
    'pos_index',
    'privacy_policy',
    'reconciliar_pago',
    'registrar_venta',
    'terms_of_service',
]
