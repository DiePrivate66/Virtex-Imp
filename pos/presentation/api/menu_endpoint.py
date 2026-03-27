from __future__ import annotations

from django.shortcuts import render

from pos.application.web_orders import get_menu_page_context


def handle_menu_request(request):
    """Renderiza la vista principal de la PWA del cliente."""
    return render(request, 'pedidos/menu.html', get_menu_page_context())
