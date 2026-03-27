from __future__ import annotations

import unicodedata
from typing import Optional


def parse_customer_confirmation(text: str) -> Optional[str]:
    normalized = (text or '').strip().lower()
    if not normalized:
        return None

    normalized = ''.join(
        ch for ch in unicodedata.normalize('NFD', normalized) if unicodedata.category(ch) != 'Mn'
    )

    yes_tokens = {
        'si',
        'ok',
        'dale',
        'confirmar',
        'confirmo',
        'acepto',
        'aceptar',
        '1',
        'confirmar_si',
        'confirmar_pedido',
        'confirmar pedido',
    }
    no_tokens = {
        'no',
        'cancelar',
        'cancelo',
        'rechazo',
        'rechazar',
        '2',
        'confirmar_no',
        'cancelar_pedido',
        'cancelar pedido',
    }

    if normalized in yes_tokens:
        return 'ACEPTADA'
    if normalized in no_tokens:
        return 'RECHAZADA'
    return None
