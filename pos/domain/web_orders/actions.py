from __future__ import annotations

from typing import Final

ACTION_ACCEPT_ORDER: Final = 'accept_order'
ACTION_MARK_IN_TRANSIT: Final = 'mark_in_transit'
ACTION_MARK_READY: Final = 'mark_ready'
ACTION_CANCEL_ORDER: Final = 'cancel_order'

VALID_PANEL_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        ACTION_ACCEPT_ORDER,
        ACTION_MARK_IN_TRANSIT,
        ACTION_MARK_READY,
        ACTION_CANCEL_ORDER,
    }
)
