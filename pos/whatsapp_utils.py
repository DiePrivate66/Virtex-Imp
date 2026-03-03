import re
import unicodedata
from typing import Optional


def normalize_phone_to_e164(phone: str, default_country_code: str = '593') -> str:
    if not phone:
        return ''

    p = (phone or '').strip()
    p = p.replace('whatsapp:', '').replace('+', '')
    p = re.sub(r'\D', '', p)
    if not p:
        return ''

    if p.startswith(default_country_code):
        return f'+{p}'
    if p.startswith('0') and len(p) >= 10:
        return f'+{default_country_code}{p[1:]}'
    if len(p) >= 9:
        return f'+{default_country_code}{p}'
    return f'+{p}'


def parse_customer_confirmation(text: str) -> Optional[str]:
    t = (text or '').strip().lower()
    if not t:
        return None

    t = ''.join(ch for ch in unicodedata.normalize('NFD', t) if unicodedata.category(ch) != 'Mn')

    yes_tokens = {
        'si',
        'ok',
        'dale',
        'confirmar',
        'confirmo',
        'acepto',
        'aceptar',
        '1',
    }
    no_tokens = {
        'no',
        'cancelar',
        'cancelo',
        'rechazo',
        'rechazar',
        '2',
    }

    if t in yes_tokens:
        return 'ACEPTADA'
    if t in no_tokens:
        return 'RECHAZADA'
    return None