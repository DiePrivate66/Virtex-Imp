from __future__ import annotations

from .actions import apply_web_order_action
from .commands import WebOrderTransitionError, set_delivery_cost
from .updates import build_web_order_update_request


def apply_web_order_update(data: dict):
    update_request = build_web_order_update_request(data)

    if update_request.updates_delivery_cost:
        return set_delivery_cost(update_request.pedido_id, update_request.delivery_cost)

    if not update_request.action_name:
        raise WebOrderTransitionError('Accion invalida', status_code=400)

    return apply_web_order_action(update_request.pedido_id, update_request.action_name)
