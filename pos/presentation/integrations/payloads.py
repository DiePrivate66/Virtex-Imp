from __future__ import annotations

import json


def parse_json_body(request) -> dict:
    try:
        data = json.loads(request.body or '{}')
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}
