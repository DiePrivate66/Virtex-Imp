"""Shared domain helpers used across Bosco domains."""

from .phones import normalize_phone_to_e164
from .operational_invariants import (
    build_cash_movement_scope_fields,
    build_inventory_movement_scope_fields,
)
from .sale_invariants import (
    backfill_sale_payment_fields_from_legacy,
    build_sale_actor_snapshot_fields,
    build_sale_detail_fields,
    build_sale_payment_fields,
    build_sale_scope_fields,
)

__all__ = [
    'build_cash_movement_scope_fields',
    'build_inventory_movement_scope_fields',
    'backfill_sale_payment_fields_from_legacy',
    'build_sale_actor_snapshot_fields',
    'build_sale_detail_fields',
    'build_sale_payment_fields',
    'build_sale_scope_fields',
    'normalize_phone_to_e164',
]
