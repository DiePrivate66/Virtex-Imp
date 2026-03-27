from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .whatsapp_endpoint import handle_whatsapp_webhook_request


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def whatsapp_webhook(request):
    return handle_whatsapp_webhook_request(request)
