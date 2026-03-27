"""Presentation layer for delivery views."""

from .views import (
    api_fijar_precio,
    delivery_claim_form,
    delivery_claim_submit,
    delivery_delivered_form,
    delivery_delivered_submit,
    delivery_in_transit_form,
    delivery_in_transit_submit,
    delivery_portal,
    delivery_quote_form,
    delivery_quote_submit,
)

__all__ = [
    'api_fijar_precio',
    'delivery_claim_form',
    'delivery_claim_submit',
    'delivery_delivered_form',
    'delivery_delivered_submit',
    'delivery_in_transit_form',
    'delivery_in_transit_submit',
    'delivery_portal',
    'delivery_quote_form',
    'delivery_quote_submit',
]
