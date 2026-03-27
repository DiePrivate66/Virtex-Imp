from __future__ import annotations

from django.http import JsonResponse


def ensure_authenticated(request):
    if request.user.is_authenticated:
        return None
    return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)
