from __future__ import annotations

import re

from django.contrib.auth.models import Group, User
from django.db import transaction
from django.utils import timezone

from pos.models import Asistencia, Empleado


class StaffError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def save_employee(data: dict) -> Empleado:
    empleado_id = data.get('id')
    cedula_raw = str(data.get('cedula') or '').strip()
    cedula_limpia = re.sub(r'\D', '', cedula_raw)
    cedula_valor = cedula_limpia if cedula_limpia else None

    if cedula_valor and not re.fullmatch(r'\d{10}', cedula_valor):
        raise StaffError('La cedula debe tener exactamente 10 digitos')

    pin = data.get('pin')
    if not pin:
        raise StaffError('El PIN es requerido')

    if empleado_id:
        empleado = Empleado.objects.get(id=empleado_id)
        if pin != empleado.pin and Empleado.objects.filter(pin=pin).exclude(id=empleado.id).exists():
            raise StaffError('El PIN ya esta en uso por otro empleado')
    else:
        if Empleado.objects.filter(pin=pin).exists():
            raise StaffError('El PIN ya esta en uso')
        empleado = Empleado(pin=pin)

    empleado.nombre = data.get('nombre')
    empleado.cedula = cedula_valor
    empleado.telefono = data.get('telefono')
    empleado.direccion = data.get('direccion')
    empleado.rol = data.get('rol')
    empleado.pin = pin
    empleado.activo = data.get('activo', True)
    empleado.save()

    sync_employee_user(empleado)
    return empleado


@transaction.atomic
def register_attendance(pin: str | None, accion: str | None) -> str:
    try:
        empleado = Empleado.objects.get(pin=pin, activo=True)
    except Empleado.DoesNotExist as exc:
        raise StaffError('PIN no encontrado') from exc

    hoy = timezone.localtime().date()

    if accion == 'ENTRADA':
        existe = Asistencia.objects.filter(
            empleado=empleado,
            fecha=hoy,
            hora_salida__isnull=True,
        ).exists()
        if existe:
            raise StaffError(f'Hola {empleado.nombre}, ya marcaste entrada hoy.')

        Asistencia.objects.create(empleado=empleado)
        return f'Bienvenido/a {empleado.nombre}. Entrada registrada.'

    if accion == 'SALIDA':
        asistencia = Asistencia.objects.filter(
            empleado=empleado,
            fecha=hoy,
            hora_salida__isnull=True,
        ).last()
        if not asistencia:
            raise StaffError(f'Error: No tienes una entrada registrada hoy o ya marcaste salida.')

        asistencia.registrar_salida()
        return f'Hasta luego {empleado.nombre}. Salida registrada.'

    raise StaffError('Accion no valida')


def sync_employee_user(empleado: Empleado) -> None:
    if empleado.rol in ['ADMIN', 'CAJERO']:
        user = _get_or_create_pos_user(empleado)
        empleado.usuario = user
        empleado.save(update_fields=['usuario'])
        _sync_pos_groups(user, empleado.rol)
        return

    if empleado.usuario:
        _clear_pos_groups(empleado.usuario)
        empleado.usuario = None
        empleado.save(update_fields=['usuario'])


def _get_or_create_pos_user(empleado: Empleado) -> User:
    username = f'emp_{empleado.pin}'
    user = empleado.usuario

    if not user:
        user, _created = User.objects.get_or_create(username=username)

    user.username = username
    user.first_name = empleado.nombre.split()[0] if empleado.nombre else ''
    user.set_password(empleado.pin)
    user.save()
    return user


def _sync_pos_groups(user: User, rol: str) -> None:
    admin_group, _ = Group.objects.get_or_create(name='Admin')
    cajero_group, _ = Group.objects.get_or_create(name='Cajero')
    user.groups.remove(admin_group, cajero_group)
    if rol == 'ADMIN':
        user.groups.add(admin_group)
    elif rol == 'CAJERO':
        user.groups.add(cajero_group)


def _clear_pos_groups(user: User) -> None:
    admin_group, _ = Group.objects.get_or_create(name='Admin')
    cajero_group, _ = Group.objects.get_or_create(name='Cajero')
    user.groups.remove(admin_group, cajero_group)
