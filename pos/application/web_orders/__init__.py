"""Web orders use cases."""

from .actions import apply_web_order_action
from .commands import (
    WebOrderError,
    WebOrderTransitionError,
    accept_web_order,
    cancel_web_order,
    create_web_order,
    get_web_order,
    mark_order_in_transit,
    mark_order_ready,
    require_transition,
    set_delivery_cost,
)
from .panel import build_web_orders_payload, get_web_orders_panel_context, timed_out_quote_count
from .queries import (
    build_product_catalog_payload,
    build_web_orders_payload as build_web_orders_query_payload,
    get_closed_store_message,
    get_menu_page_context,
    get_web_orders_panel_context as get_web_orders_panel_query_context,
    store_is_open,
    timed_out_quote_count as timed_out_quote_query_count,
)
from .transitions import apply_web_order_update
from .updates import WebOrderUpdateRequest, build_web_order_update_request

__all__ = [
    'WebOrderError',
    'WebOrderTransitionError',
    'accept_web_order',
    'apply_web_order_action',
    'apply_web_order_update',
    'build_product_catalog_payload',
    'build_web_orders_payload',
    'build_web_orders_query_payload',
    'build_web_order_update_request',
    'cancel_web_order',
    'create_web_order',
    'get_closed_store_message',
    'get_web_order',
    'get_menu_page_context',
    'get_web_orders_panel_context',
    'get_web_orders_panel_query_context',
    'mark_order_in_transit',
    'mark_order_ready',
    'require_transition',
    'set_delivery_cost',
    'store_is_open',
    'timed_out_quote_count',
    'timed_out_quote_query_count',
    'WebOrderUpdateRequest',
]
