from __future__ import annotations

from django.http import JsonResponse

from pos.application.integrations import get_integrations_health_payload

from ._common import ensure_authenticated


def handle_integrations_health_request(request):
    auth_error = ensure_authenticated(request)
    if auth_error:
        return auth_error
    return JsonResponse(get_integrations_health_payload())
