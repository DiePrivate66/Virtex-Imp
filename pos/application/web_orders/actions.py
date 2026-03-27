from __future__ import annotations

from pos.domain.web_orders import (
    ACTION_ACCEPT_ORDER,
    ACTION_CANCEL_ORDER,
    ACTION_MARK_IN_TRANSIT,
    ACTION_MARK_READY,
)
from pos.models import Venta

from .commands import (
    WebOrderTransitionError,
    accept_web_order,
    cancel_web_order,
    mark_order_in_transit,
    mark_order_ready,
)


ACTION_HANDLERS = {
    ACTION_ACCEPT_ORDER: accept_web_order,
    ACTION_MARK_IN_TRANSIT: mark_order_in_transit,
    ACTION_MARK_READY: mark_order_ready,
    ACTION_CANCEL_ORDER: cancel_web_order,
}


def apply_web_order_action(pedido_id, action_name: str) -> Venta:
    try:
        handler = ACTION_HANDLERS[action_name]
    except KeyError as exc:
        raise WebOrderTransitionError('Accion invalida', status_code=400) from exc
    return handler(pedido_id)
