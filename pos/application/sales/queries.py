from __future__ import annotations

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
    }
