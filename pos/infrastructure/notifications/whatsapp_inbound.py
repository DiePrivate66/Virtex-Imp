from __future__ import annotations

import json
from typing import Optional


def extract_inbound_whatsapp(request) -> Optional[dict]:
    try:
        payload = json.loads(request.body or '{}')
    except Exception:
        return None

    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value', {}) or {}
            messages = value.get('messages') or []
            for msg in messages:
                from_raw = msg.get('from', '')
                message_id = msg.get('id')
                msg_type = msg.get('type')
                body = ''
                button_text = ''
                button_payload = ''

                if msg_type == 'text':
                    body = (msg.get('text') or {}).get('body', '') or ''
                elif msg_type == 'button':
                    btn = msg.get('button') or {}
                    button_text = btn.get('text', '') or ''
                    button_payload = btn.get('payload', '') or ''
                elif msg_type == 'interactive':
                    interactive = msg.get('interactive') or {}
                    itype = interactive.get('type')
                    if itype == 'button_reply':
                        reply = interactive.get('button_reply') or {}
                        button_text = reply.get('title', '') or ''
                        button_payload = reply.get('id', '') or ''
                    elif itype == 'list_reply':
                        reply = interactive.get('list_reply') or {}
                        button_text = reply.get('title', '') or ''
                        button_payload = reply.get('id', '') or ''

                return {
                    'from_raw': from_raw,
                    'body': body.strip(),
                    'button_text': button_text.strip(),
                    'button_payload': button_payload.strip(),
                    'message_sid': message_id,
                    'raw_payload': payload,
                }
    return None
