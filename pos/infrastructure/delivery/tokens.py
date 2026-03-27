from __future__ import annotations

from django.conf import settings
from django.core import signing

QUOTE_SIGNER_SALT = 'pos.delivery.quote'
CLAIM_SIGNER_SALT = 'pos.delivery.claim'
IN_TRANSIT_SIGNER_SALT = 'pos.delivery.in_transit'
DELIVERED_SIGNER_SALT = 'pos.delivery.delivered'


def make_delivery_quote_token(venta_id: int, empleado_id: int) -> str:
    payload = {'venta_id': venta_id, 'empleado_id': empleado_id}
    return signing.dumps(payload, salt=QUOTE_SIGNER_SALT)


def read_delivery_quote_token(token: str):
    max_age = settings.DELIVERY_QUOTE_TOKEN_MAX_AGE_SECONDS
    return signing.loads(token, salt=QUOTE_SIGNER_SALT, max_age=max_age)


def make_delivery_claim_token(venta_id: int) -> str:
    return signing.dumps({'venta_id': venta_id}, salt=CLAIM_SIGNER_SALT)


def read_delivery_claim_token(token: str):
    max_age = getattr(settings, 'DELIVERY_CLAIM_TOKEN_MAX_AGE_SECONDS', 1800)
    return signing.loads(token, salt=CLAIM_SIGNER_SALT, max_age=max_age)


def make_delivery_in_transit_token(venta_id: int, empleado_id: int) -> str:
    payload = {'venta_id': venta_id, 'empleado_id': empleado_id}
    return signing.dumps(payload, salt=IN_TRANSIT_SIGNER_SALT)


def read_delivery_in_transit_token(token: str):
    max_age = getattr(settings, 'DELIVERY_IN_TRANSIT_TOKEN_MAX_AGE_SECONDS', 21600)
    return signing.loads(token, salt=IN_TRANSIT_SIGNER_SALT, max_age=max_age)


def make_delivery_delivered_token(venta_id: int, empleado_id: int) -> str:
    payload = {'venta_id': venta_id, 'empleado_id': empleado_id}
    return signing.dumps(payload, salt=DELIVERED_SIGNER_SALT)


def read_delivery_delivered_token(token: str):
    max_age = getattr(settings, 'DELIVERY_DELIVERED_TOKEN_MAX_AGE_SECONDS', 21600)
    return signing.loads(token, salt=DELIVERED_SIGNER_SALT, max_age=max_age)
