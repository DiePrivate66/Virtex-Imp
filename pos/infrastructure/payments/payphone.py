from __future__ import annotations

import json
import logging
from urllib import error as urlerror
from urllib import request as urlrequest

from django.conf import settings

logger = logging.getLogger(__name__)


class PayPhoneError(Exception):
    pass


def payphone_web_checkout_enabled() -> bool:
    return bool(
        getattr(settings, 'PAYPHONE_ENABLED', False)
        and getattr(settings, 'PAYPHONE_TOKEN', '')
        and getattr(settings, 'PAYPHONE_STORE_ID', '')
    )


def prepare_payphone_checkout(payload: dict) -> dict:
    return _payphone_request('/api/button/Prepare', payload)


def confirm_payphone_transaction(*, payphone_id: int | str, client_transaction_id: str) -> dict:
    return _payphone_request(
        '/api/button/V2/Confirm',
        {
            'id': int(payphone_id),
            'clientTxId': client_transaction_id,
        },
    )


def _payphone_request(path: str, payload: dict) -> dict:
    if not payphone_web_checkout_enabled():
        raise PayPhoneError('PayPhone no esta configurado')

    api_base = getattr(settings, 'PAYPHONE_API_BASE', 'https://pay.payphonetodoesposible.com').rstrip('/')
    token = getattr(settings, 'PAYPHONE_TOKEN', '')
    timeout_seconds = getattr(settings, 'PAYPHONE_TIMEOUT_SECONDS', 15)
    request_obj = urlrequest.Request(
        f'{api_base}{path}',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    try:
        with urlrequest.urlopen(request_obj, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode('utf-8') or '{}')
    except urlerror.HTTPError as exc:
        response_body = ''
        try:
            response_body = exc.read().decode('utf-8')
        except Exception:
            response_body = str(exc)
        logger.exception('PayPhone API error: %s', response_body)
        raise PayPhoneError(response_body or 'PayPhone rechazo la solicitud') from exc
    except Exception as exc:
        logger.exception('PayPhone request failed')
        raise PayPhoneError(str(exc) or 'No se pudo conectar con PayPhone') from exc
