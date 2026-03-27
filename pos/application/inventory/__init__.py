"""Inventory use cases."""

from .commands import (
    InventoryError,
    InventoryMovementResult,
    ensure_inventory_for_product,
    register_inventory_movement,
    update_inventory_configuration,
)
from .queries import (
    get_inventory_history_context,
    get_inventory_panel_context,
    get_inventory_report_context,
)

__all__ = [
    'InventoryError',
    'InventoryMovementResult',
    'ensure_inventory_for_product',
    'get_inventory_history_context',
    'get_inventory_panel_context',
    'get_inventory_report_context',
    'register_inventory_movement',
    'update_inventory_configuration',
]
