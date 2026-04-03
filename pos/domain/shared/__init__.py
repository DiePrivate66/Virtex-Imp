"""Shared domain helpers used across Bosco domains."""

from .phones import normalize_phone_to_e164
from .sale_invariants import (
    build_sale_actor_snapshot_fields,
    build_sale_payment_fields,
    build_sale_scope_fields,
)

__all__ = [
    'build_sale_actor_snapshot_fields',
    'build_sale_payment_fields',
    'build_sale_scope_fields',
    'normalize_phone_to_e164',
]
