"""Legacy app config kept for historical ``pedidos`` imports."""

from django.apps import AppConfig


class PedidosConfig(AppConfig):
    name = 'pedidos'
    verbose_name = 'Pedidos (legacy compatibility)'
