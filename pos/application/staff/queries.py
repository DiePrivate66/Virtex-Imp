from __future__ import annotations

import re

from pos.models import Empleado


def get_employee_list():
    return Empleado.objects.all().order_by('-activo', 'nombre')


def find_employee_by_id(empleado_id):
    return Empleado.objects.get(id=empleado_id)


def find_employee_by_pin(pin: str):
    return Empleado.objects.get(pin=pin, activo=True)


def normalize_identity_document(value: str | None) -> str | None:
    cedula_limpia = re.sub(r'\D', '', str(value or '').strip())
    return cedula_limpia or None
