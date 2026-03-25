from django.conf import settings
from django.core import signing

SIGNER_SALT = 'pos.delivery.quote'
CLAIM_SALT = 'pos.delivery.claim'


def make_delivery_quote_token(venta_id: int, empleado_id: int) -> str:
    data = {'venta_id': venta_id, 'empleado_id': empleado_id}
    return signing.dumps(data, salt=SIGNER_SALT)


def read_delivery_quote_token(token: str):
    max_age = settings.DELIVERY_QUOTE_TOKEN_MAX_AGE_SECONDS
    return signing.loads(token, salt=SIGNER_SALT, max_age=max_age)


def make_delivery_claim_token(venta_id: int) -> str:
    return signing.dumps({'venta_id': venta_id}, salt=CLAIM_SALT)


def read_delivery_claim_token(token: str):
    max_age = getattr(settings, 'DELIVERY_CLAIM_TOKEN_MAX_AGE_SECONDS', 1800)
    return signing.loads(token, salt=CLAIM_SALT, max_age=max_age)
