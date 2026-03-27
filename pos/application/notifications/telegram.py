"""Application facade for Telegram delivery notifications."""

from pos.infrastructure.notifications.telegram import notify_delivery_group, notify_order_claimed

__all__ = ['notify_delivery_group', 'notify_order_claimed']
