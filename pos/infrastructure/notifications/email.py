from __future__ import annotations

import json
import logging
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from django.conf import settings

logger = logging.getLogger(__name__)


class ResendEmailError(Exception):
    pass


def send_resend_email(
    *,
    subject: str,
    text_body: str,
    html_body: str,
    recipient_email: str,
    from_email: Optional[str] = None,
) -> bool:
    api_key = getattr(settings, 'RESEND_API_KEY', '')
    if not api_key:
        raise ResendEmailError('RESEND_API_KEY not configured')

    payload = {
        'from': from_email or settings.DEFAULT_FROM_EMAIL,
        'to': [recipient_email],
        'subject': subject,
        'text': text_body,
        'html': html_body,
    }

    req = urlrequest.Request(
        f"{getattr(settings, 'RESEND_API_BASE', 'https://api.resend.com').rstrip('/')}/emails",
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )

    try:
        with urlrequest.urlopen(req, timeout=getattr(settings, 'RESEND_API_TIMEOUT_SECONDS', 15)) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            message_id = body.get('id')
            if message_id:
                logger.info(
                    'Resend accepted email id=%s recipient=%s from=%s subject=%s',
                    message_id,
                    recipient_email,
                    payload['from'],
                    subject,
                )
            return bool(message_id)
    except urlerror.HTTPError as exc:
        response_body = ''
        try:
            response_body = exc.read().decode('utf-8')
        except Exception:
            response_body = str(exc)
        logger.exception('Resend API error: %s', response_body)
        raise ResendEmailError(response_body) from exc
    except Exception as exc:
        logger.exception('Resend request failed')
        raise ResendEmailError(str(exc)) from exc
