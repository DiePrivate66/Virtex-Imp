from __future__ import annotations

from django.views.decorators.http import require_GET

from .health_endpoint import handle_integrations_health_request


@require_GET
def api_integrations_health(request):
    return handle_integrations_health_request(request)
