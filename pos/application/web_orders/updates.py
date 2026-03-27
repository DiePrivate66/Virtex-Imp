from __future__ import annotations

from dataclasses import dataclass

from pos.domain.web_orders import (
    ACTION_ACCEPT_ORDER,
    ACTION_CANCEL_ORDER,
    ACTION_MARK_IN_TRANSIT,
    ACTION_MARK_READY,
    STATUS_CANCELLED,
    STATUS_IN_TRANSIT,
    STATUS_KITCHEN,
    STATUS_READY,
    VALID_PANEL_ACTIONS,
)

from .commands import WebOrderTransitionError


LEGACY_STATUS_ACTION_MAP = {
    STATUS_KITCHEN: ACTION_ACCEPT_ORDER,
    STATUS_IN_TRANSIT: ACTION_MARK_IN_TRANSIT,
    STATUS_READY: ACTION_MARK_READY,
    STATUS_CANCELLED: ACTION_CANCEL_ORDER,
}


@dataclass(frozen=True)
class WebOrderUpdateRequest:
    pedido_id: int
    action_name: str | None = None
    delivery_cost: object | None = None

    @property
    def updates_delivery_cost(self) -> bool:
        return self.delivery_cost is not None


def build_web_order_update_request(data: dict) -> WebOrderUpdateRequest:
    pedido_id = data.get('pedido_id')
    if not pedido_id:
        raise WebOrderTransitionError('Pedido no encontrado', status_code=404)

    if 'costo_envio' in data:
        return WebOrderUpdateRequest(pedido_id=pedido_id, delivery_cost=data.get('costo_envio'))

    action_name = data.get('accion')
    if action_name is not None:
        if action_name not in VALID_PANEL_ACTIONS:
            raise WebOrderTransitionError('Accion invalida', status_code=400)
        return WebOrderUpdateRequest(pedido_id=pedido_id, action_name=action_name)

    legacy_status = data.get('estado')
    legacy_action = LEGACY_STATUS_ACTION_MAP.get(legacy_status)
    if not legacy_action:
        raise WebOrderTransitionError('Estado invalido', status_code=400)

    return WebOrderUpdateRequest(pedido_id=pedido_id, action_name=legacy_action)
