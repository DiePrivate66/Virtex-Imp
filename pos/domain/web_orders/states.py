from __future__ import annotations

from typing import Final

STATUS_PENDING: Final = 'PENDIENTE'
STATUS_PENDING_QUOTE: Final = 'PENDIENTE_COTIZACION'
STATUS_KITCHEN: Final = 'COCINA'
STATUS_IN_TRANSIT: Final = 'EN_CAMINO'
STATUS_READY: Final = 'LISTO'
STATUS_CANCELLED: Final = 'CANCELADO'

ACTIVE_PANEL_STATUSES: Final[tuple[str, ...]] = (
    STATUS_PENDING,
    STATUS_PENDING_QUOTE,
    STATUS_KITCHEN,
    STATUS_IN_TRANSIT,
)

VISIBLE_PANEL_STATUSES: Final[tuple[str, ...]] = ACTIVE_PANEL_STATUSES + (
    STATUS_READY,
)

QUOTE_EDITABLE_STATUSES: Final[frozenset[str]] = frozenset(
    {
        STATUS_PENDING,
        STATUS_PENDING_QUOTE,
    }
)

TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    STATUS_KITCHEN: frozenset({STATUS_PENDING, STATUS_PENDING_QUOTE}),
    STATUS_IN_TRANSIT: frozenset({STATUS_KITCHEN}),
    STATUS_READY: frozenset({STATUS_KITCHEN, STATUS_IN_TRANSIT}),
    STATUS_CANCELLED: frozenset(
        {
            STATUS_PENDING,
            STATUS_PENDING_QUOTE,
            STATUS_KITCHEN,
            STATUS_IN_TRANSIT,
        }
    ),
}


def can_transition(current_status: str, target_status: str) -> bool:
    if current_status == target_status:
        return True
    return current_status in TRANSITIONS.get(target_status, frozenset())
