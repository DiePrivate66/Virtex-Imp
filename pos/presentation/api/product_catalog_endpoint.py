from __future__ import annotations

from django.http import JsonResponse

from pos.application.web_orders import build_product_catalog_payload


def handle_product_catalog_request(request):
    """Devuelve el catalogo de productos agrupado por categoria."""
    return JsonResponse(build_product_catalog_payload())
