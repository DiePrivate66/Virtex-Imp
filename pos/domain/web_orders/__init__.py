"""Web orders domain rules."""

from .actions import (
    ACTION_ACCEPT_ORDER,
    ACTION_CANCEL_ORDER,
    ACTION_MARK_IN_TRANSIT,
    ACTION_MARK_READY,
    VALID_PANEL_ACTIONS,
)
from .customer_confirmation import parse_customer_confirmation
from .states import (
    ACTIVE_PANEL_STATUSES,
    QUOTE_EDITABLE_STATUSES,
    STATUS_CANCELLED,
    STATUS_IN_TRANSIT,
    STATUS_KITCHEN,
    STATUS_PENDING,
    STATUS_PENDING_QUOTE,
    STATUS_READY,
    VISIBLE_PANEL_STATUSES,
    can_transition,
)

__all__ = [
    'ACTION_ACCEPT_ORDER',
    'ACTION_CANCEL_ORDER',
    'ACTION_MARK_IN_TRANSIT',
    'ACTION_MARK_READY',
    'ACTIVE_PANEL_STATUSES',
    'QUOTE_EDITABLE_STATUSES',
    'STATUS_CANCELLED',
    'STATUS_IN_TRANSIT',
    'STATUS_KITCHEN',
    'STATUS_PENDING',
    'STATUS_PENDING_QUOTE',
    'STATUS_READY',
    'VALID_PANEL_ACTIONS',
    'VISIBLE_PANEL_STATUSES',
    'can_transition',
    'parse_customer_confirmation',
]
