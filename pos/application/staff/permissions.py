from __future__ import annotations

from django.contrib.auth.models import Group

ALLOWED_POS_GROUPS = {'Cajero', 'Admin'}


def ensure_pos_groups_for_user(user) -> None:
    empleado = getattr(user, 'empleado', None)
    if not empleado:
        return

    admin_group, _ = Group.objects.get_or_create(name='Admin')
    cajero_group, _ = Group.objects.get_or_create(name='Cajero')
    current_names = set(user.groups.values_list('name', flat=True))

    if empleado.rol == 'ADMIN':
        if current_names != {'Admin'}:
            user.groups.remove(admin_group, cajero_group)
            user.groups.add(admin_group)
        return

    if empleado.rol == 'CAJERO':
        if current_names != {'Cajero'}:
            user.groups.remove(admin_group, cajero_group)
            user.groups.add(cajero_group)
        return

    if current_names.intersection(ALLOWED_POS_GROUPS):
        user.groups.remove(admin_group, cajero_group)


def user_is_pos_operator(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True

    ensure_pos_groups_for_user(user)
    return user.groups.filter(name__in=ALLOWED_POS_GROUPS).exists()
