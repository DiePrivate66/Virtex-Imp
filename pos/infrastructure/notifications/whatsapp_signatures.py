from __future__ import annotations

import hashlib
import hmac

from django.conf import settings


def validate_meta_signature(request) -> bool:
    if not getattr(settings, 'META_SIGNATURE_VALIDATION', False):
        return True
    app_secret = getattr(settings, 'META_WHATSAPP_APP_SECRET', '')
    if not app_secret:
        return False
    signature = request.headers.get('X-Hub-Signature-256', '')
    if not signature.startswith('sha256='):
        return False
    expected = hmac.new(app_secret.encode('utf-8'), request.body, hashlib.sha256).hexdigest()
    received = signature.split('=', 1)[1].strip()
    return hmac.compare_digest(expected, received)


def validate_whatsapp_signature(request) -> bool:
    return validate_meta_signature(request)
