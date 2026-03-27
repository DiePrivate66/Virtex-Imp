from __future__ import annotations

from django.conf import settings

from .whatsapp_errors import WhatsAppIntegrationError


def verify_meta_webhook_subscription(mode: str, verify_token: str, challenge: str) -> str:
    expected = getattr(settings, 'META_WHATSAPP_VERIFY_TOKEN', '')
    if mode == 'subscribe' and verify_token and verify_token == expected:
        return challenge
    raise WhatsAppIntegrationError('invalid verify token', status_code=403)
