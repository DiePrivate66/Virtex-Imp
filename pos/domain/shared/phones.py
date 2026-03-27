from __future__ import annotations

import re


def normalize_phone_to_e164(phone: str, default_country_code: str = '593') -> str:
    if not phone:
        return ''

    normalized = (phone or '').strip()
    normalized = normalized.replace('whatsapp:', '').replace('+', '')
    normalized = re.sub(r'\D', '', normalized)
    if not normalized:
        return ''

    if normalized.startswith(default_country_code):
        return f'+{normalized}'
    if normalized.startswith('0') and len(normalized) >= 10:
        return f'+{default_country_code}{normalized[1:]}'
    if len(normalized) >= 9:
        return f'+{default_country_code}{normalized}'
    return f'+{normalized}'
