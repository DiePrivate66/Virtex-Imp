from __future__ import annotations

from django.http import HttpResponse, JsonResponse

from pos.application.notifications import send_whatsapp_text


def webhook_challenge_response(challenge: str):
    return HttpResponse(challenge, status=200)


def webhook_ack_response(phone_e164: str, body: str):
    send_whatsapp_text(phone_e164, body)
    return JsonResponse({'status': 'ok'})
