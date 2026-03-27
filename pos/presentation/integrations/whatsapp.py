"""Compatibility facade for WhatsApp HTTP endpoints."""

from .whatsapp_confirmation import confirmar_venta_whatsapp
from .whatsapp_webhook import whatsapp_webhook

__all__ = ['confirmar_venta_whatsapp', 'whatsapp_webhook']
