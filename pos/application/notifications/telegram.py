"""Application facade for Telegram delivery notifications."""

from pos.infrastructure.notifications.telegram import (
    notify_customer_reported_received,
    notify_delivery_group,
    notify_order_claimed,
)

__all__ = ['notify_customer_reported_received', 'notify_delivery_group', 'notify_order_claimed']
