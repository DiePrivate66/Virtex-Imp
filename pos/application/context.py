from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.hashers import make_password
from django.core.exceptions import PermissionDenied
from django.utils.text import slugify

from pos.models import (
    DEFAULT_LOCATION_TIMEZONE,
    Location,
    LocationAssignment,
    Organization,
    OrganizationMembership,
    PersonProfile,
    StaffProfile,
)


@dataclass(frozen=True)
class LocationContext:
    location: Location

    @property
    def organization(self):
        return self.location.organization

    @property
    def timezone_name(self):
        return self.location.timezone or DEFAULT_LOCATION_TIMEZONE

    @property
    def operating_day_ends_at(self):
        return self.location.operating_day_ends_at


def build_location_context(location: Location) -> LocationContext:
    return LocationContext(location=location)


def get_default_catalog_organization() -> Organization:
    return Location.get_or_create_default().organization


def resolve_catalog_organization_for_user(user) -> Organization:
    if user and user.is_authenticated:
        return resolve_location_for_user(user).organization
    return get_default_catalog_organization()


def ensure_person_profile_for_user(user) -> PersonProfile:
    defaults = {
        'legal_name': user.get_full_name().strip() or user.username,
    }
    person, created = PersonProfile.objects.get_or_create(user=user, defaults=defaults)
    if not created and not person.legal_name:
        person.legal_name = defaults['legal_name']
        person.save(update_fields=['legal_name'])
    return person


def _resolve_membership_role(user) -> str:
    if user.is_superuser:
        return OrganizationMembership.Role.OWNER
    group_names = {group.name.upper() for group in user.groups.all()}
    if 'ADMIN' in group_names:
        return OrganizationMembership.Role.ADMIN
    if 'MANAGER' in group_names:
        return OrganizationMembership.Role.MANAGER
    return OrganizationMembership.Role.STAFF


def ensure_membership_for_user(user, *, location: Location | None = None) -> OrganizationMembership:
    location = location or Location.get_or_create_default()
    membership, created = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=location.organization,
        defaults={
            'role': _resolve_membership_role(user),
            'active': True,
        },
    )
    if not created and not membership.active:
        membership.active = True
        membership.save(update_fields=['active'])
    ensure_person_profile_for_user(user)
    return membership


def _alias_seed_for_user(user) -> str:
    empleado = getattr(user, 'empleado', None)
    raw_value = ''
    if empleado and empleado.nombre:
        raw_value = empleado.nombre.split()[0]
    elif user.first_name:
        raw_value = user.first_name
    else:
        raw_value = user.username
    seed = slugify(raw_value or '').replace('-', '')
    return (seed or f'user{user.id}')[:32]


def _build_unique_alias(location: Location, desired_alias: str, *, staff_profile=None) -> str:
    seed = slugify(desired_alias or '').replace('-', '') or 'staff'
    base = seed[:28] or 'staff'
    alias = base
    suffix = 1
    queryset = LocationAssignment.objects.filter(location=location, active=True)
    if staff_profile is not None:
        queryset = queryset.exclude(staff_profile=staff_profile)

    while queryset.filter(alias_normalized=alias.lower()).exists():
        suffix += 1
        alias = f'{base[: max(1, 28 - len(str(suffix)))]}{suffix}'
    return alias


def ensure_staff_profile_for_user(user, *, location: Location | None = None) -> StaffProfile:
    location = location or Location.get_or_create_default()
    membership = ensure_membership_for_user(user, location=location)
    defaults = {
        'operational_role': StaffProfile.OperationalRole.CAJERO,
        'active': True,
    }

    empleado = getattr(user, 'empleado', None)
    if empleado:
        defaults.update(
            {
                'work_phone': empleado.telefono or '',
                'operational_role': empleado.rol or StaffProfile.OperationalRole.OTRO,
                'requires_pin_setup': False if empleado.pin else True,
                'pin_hash': make_password(empleado.pin) if empleado.pin else '',
            }
        )

    staff_profile, created = StaffProfile.objects.get_or_create(membership=membership, defaults=defaults)
    update_fields = []
    if not created and not staff_profile.active:
        staff_profile.active = True
        update_fields.append('active')
    if empleado and empleado.telefono and staff_profile.work_phone != empleado.telefono:
        staff_profile.work_phone = empleado.telefono
        update_fields.append('work_phone')
    if empleado and empleado.rol and staff_profile.operational_role != empleado.rol:
        staff_profile.operational_role = empleado.rol
        update_fields.append('operational_role')
    if empleado and empleado.pin and not staff_profile.pin_hash:
        staff_profile.pin_hash = make_password(empleado.pin)
        staff_profile.requires_pin_setup = False
        update_fields.extend(['pin_hash', 'requires_pin_setup'])
    if update_fields:
        staff_profile.save(update_fields=list(dict.fromkeys(update_fields)))

    ensure_location_assignment_for_staff(staff_profile, location=location)
    return staff_profile


def ensure_location_assignment_for_staff(
    staff_profile: StaffProfile,
    *,
    location: Location | None = None,
    alias: str | None = None,
) -> LocationAssignment:
    location = location or Location.get_or_create_default()
    assignment = LocationAssignment.objects.filter(
        staff_profile=staff_profile,
        location=location,
    ).first()
    if assignment:
        if not assignment.active:
            assignment.active = True
            assignment.save(update_fields=['active'])
        return assignment

    desired_alias = alias or _alias_seed_for_user(staff_profile.user)
    unique_alias = _build_unique_alias(location, desired_alias, staff_profile=staff_profile)
    return LocationAssignment.objects.create(
        staff_profile=staff_profile,
        location=location,
        alias=unique_alias,
        active=True,
    )


def resolve_location_for_user(user, *, location_uuid=None, allow_default: bool = True) -> Location:
    if location_uuid:
        location = (
            Location.objects.select_related('organization')
            .filter(uuid=location_uuid, active=True, organization__active=True)
            .first()
        )
        if not location:
            raise PermissionDenied('Sucursal no valida')
        if user and user.is_authenticated:
            membership = OrganizationMembership.objects.filter(
                user=user,
                organization=location.organization,
                active=True,
                organization__active=True,
            ).exists()
            if not membership:
                raise PermissionDenied('No tienes acceso a esta sucursal')
        return location

    if user and user.is_authenticated:
        assignment = (
            LocationAssignment.objects.select_related('location__organization')
            .filter(
                staff_profile__membership__user=user,
                staff_profile__membership__active=True,
                staff_profile__active=True,
                active=True,
                location__active=True,
                location__organization__active=True,
            )
            .order_by('id')
            .first()
        )
        if assignment:
            return assignment.location

    if allow_default:
        return Location.get_or_create_default()

    raise PermissionDenied('No existe una sucursal activa para este usuario')
