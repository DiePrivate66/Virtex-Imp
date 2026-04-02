from __future__ import annotations

from django.conf import settings

from pos.ledger_registry import MIN_SUPPORTED_QUEUE_SCHEMA, REGISTRY_VERSION, get_registry_hash
from pos.application.cash_register import get_open_cash_register_for_user
from pos.models import Categoria, Producto


def get_user_open_cash_register(user):
    return get_open_cash_register_for_user(user)


def get_pos_home_context(user):
    return {
        'categorias': Categoria.objects.all(),
        'productos': Producto.objects.filter(activo=True),
        'caja': get_user_open_cash_register(user),
        'rol': getattr(getattr(user, 'empleado', None), 'rol', 'OTRO'),
        'ledger_registry_hash': get_registry_hash(),
        'ledger_registry_version': REGISTRY_VERSION,
        'ledger_min_supported_queue_schema': MIN_SUPPORTED_QUEUE_SCHEMA,
        'ledger_client_app_version': getattr(settings, 'LEDGER_WEB_CLIENT_APP_VERSION', 'pos-web'),
    }
