from __future__ import annotations

from django.utils import timezone


def touch_conversation_inbound(conversation):
    conversation.last_inbound_at = timezone.now()
    conversation.save(update_fields=['last_inbound_at'])


def touch_conversation_outbound(conversation):
    conversation.last_outbound_at = timezone.now()
    conversation.save(update_fields=['last_outbound_at'])
