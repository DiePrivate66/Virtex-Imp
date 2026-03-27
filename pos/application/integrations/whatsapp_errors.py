from __future__ import annotations

from dataclasses import dataclass


class WhatsAppIntegrationError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class WhatsAppWebhookAck:
    phone_e164: str
    body: str
