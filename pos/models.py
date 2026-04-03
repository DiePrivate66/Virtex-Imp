from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
import uuid

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from pos.ledger_registry import (
    MIN_SUPPORTED_QUEUE_SCHEMA,
    REGISTRY_VERSION,
    get_registry_hash,
    get_system_account_defaults_map,
    get_system_account_definitions,
)


DEFAULT_LOCATION_TIMEZONE = 'America/Guayaquil'
DEFAULT_OPERATING_DAY_ENDS_AT = time(hour=4, minute=0)

LEGACY_TO_V2_PAYMENT_STATUS = {
    'PENDIENTE': 'PENDING',
    'APROBADO': 'PAID',
    'RECHAZADO': 'FAILED',
    'ANULADO': 'VOIDED',
}
V2_TO_LEGACY_PAYMENT_STATUS = {
    'PENDING': 'PENDIENTE',
    'PAID': 'APROBADO',
    'FAILED': 'RECHAZADO',
    'VOIDED': 'ANULADO',
}


def _slug_candidate(value: str, fallback: str) -> str:
    candidate = slugify(value or '').strip('-')
    return candidate or fallback


def normalize_alias(value: str | None) -> str:
    parts = str(value or '').strip().lower().split()
    return ' '.join(parts)


def _resolve_zone(timezone_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or DEFAULT_LOCATION_TIMEZONE)
    except Exception:
        return ZoneInfo(DEFAULT_LOCATION_TIMEZONE)


def compute_operating_day(
    *,
    timestamp,
    timezone_name: str | None,
    operating_day_ends_at: time | None,
):
    effective_timestamp = timestamp or timezone.now()
    local_dt = timezone.localtime(effective_timestamp, _resolve_zone(timezone_name))
    cutoff = operating_day_ends_at or DEFAULT_OPERATING_DAY_ENDS_AT
    operating_day = local_dt.date()
    if local_dt.timetz().replace(tzinfo=None) < cutoff:
        operating_day -= timedelta(days=1)
    return operating_day


# --- IDENTIDAD / TENANCY V2 ---
class PersonProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='person_profile')
    legal_name = models.CharField(max_length=200, blank=True)
    cedula = models.CharField(max_length=20, blank=True, unique=True, null=True)

    def __str__(self):
        return self.legal_name or self.user.get_full_name() or self.user.username


class Organization(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _slug_candidate(self.name, 'organization')
        super().save(*args, **kwargs)

    @classmethod
    def get_or_create_default(cls):
        return cls.objects.get_or_create(
            slug='legacy-default',
            defaults={
                'name': 'Legacy Default Organization',
                'active': True,
            },
        )[0]

    def __str__(self):
        return self.name


class Location(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='locations')
    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=200)
    timezone = models.CharField(max_length=64, default=DEFAULT_LOCATION_TIMEZONE)
    operating_day_ends_at = models.TimeField(default=DEFAULT_OPERATING_DAY_ENDS_AT)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['organization__name', 'name']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'slug'], name='uq_location_slug_per_org'),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _slug_candidate(self.name, 'location')
        super().save(*args, **kwargs)

    @classmethod
    def get_or_create_default(cls):
        organization = Organization.get_or_create_default()
        return cls.objects.get_or_create(
            organization=organization,
            slug='principal',
            defaults={
                'name': 'Principal',
                'timezone': DEFAULT_LOCATION_TIMEZONE,
                'operating_day_ends_at': DEFAULT_OPERATING_DAY_ENDS_AT,
                'active': True,
            },
        )[0]

    def __str__(self):
        return f'{self.organization.name} / {self.name}'


class OrganizationMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = 'OWNER', 'Owner'
        ADMIN = 'ADMIN', 'Admin'
        MANAGER = 'MANAGER', 'Manager'
        STAFF = 'STAFF', 'Staff'

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='organization_memberships')
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memberships')
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.STAFF)
    active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'organization'], name='uq_membership_user_org'),
        ]

    def __str__(self):
        return f'{self.user.username} @ {self.organization.name} ({self.role})'


class StaffProfile(models.Model):
    class OperationalRole(models.TextChoices):
        ADMIN = 'ADMIN', 'Administrador'
        MANAGER = 'MANAGER', 'Manager'
        CAJERO = 'CAJERO', 'Cajero'
        COCINA = 'COCINA', 'Cocina'
        MESERO = 'MESERO', 'Mesero'
        DELIVERY = 'DELIVERY', 'Delivery'
        OTRO = 'OTRO', 'Otro'

    membership = models.OneToOneField(
        OrganizationMembership,
        on_delete=models.CASCADE,
        related_name='staff_profile',
    )
    work_phone = models.CharField(max_length=20, blank=True)
    work_email = models.EmailField(blank=True)
    operational_role = models.CharField(
        max_length=20,
        choices=OperationalRole.choices,
        default=OperationalRole.OTRO,
    )
    pin_hash = models.CharField(max_length=128, blank=True)
    pin_failed_attempts = models.PositiveIntegerField(default=0)
    pin_blocked_until = models.DateTimeField(null=True, blank=True)
    requires_pin_setup = models.BooleanField(default=True)
    setup_token_hash = models.CharField(max_length=128, blank=True)
    setup_token_expires_at = models.DateTimeField(null=True, blank=True)
    setup_failed_attempts = models.PositiveIntegerField(default=0)
    setup_blocked_until = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['membership__organization__name', 'membership__user__username']

    @property
    def user(self):
        return self.membership.user

    @property
    def organization(self):
        return self.membership.organization

    @property
    def display_name(self):
        person = getattr(self.user, 'person_profile', None)
        if person and person.legal_name:
            return person.legal_name
        full_name = self.user.get_full_name().strip()
        return full_name or self.user.username

    def __str__(self):
        return f'{self.display_name} ({self.organization.name})'


class LocationAssignment(models.Model):
    staff_profile = models.ForeignKey(StaffProfile, on_delete=models.CASCADE, related_name='assignments')
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='assignments')
    alias = models.CharField(max_length=40)
    alias_normalized = models.CharField(max_length=40, editable=False, db_index=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['location__name', 'alias']
        constraints = [
            models.UniqueConstraint(
                fields=['location', 'alias_normalized'],
                condition=Q(active=True),
                name='uq_active_location_alias_normalized',
            ),
        ]

    def clean(self):
        self.alias_normalized = normalize_alias(self.alias)
        if self.location_id and self.staff_profile_id:
            if self.staff_profile.membership.organization_id != self.location.organization_id:
                raise ValidationError('La asignacion debe pertenecer a la misma organizacion de la sucursal.')

    def save(self, *args, **kwargs):
        self.alias_normalized = normalize_alias(self.alias)
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.location.name} / {self.alias}'


# --- PERFIL DE USUARIO LEGACY (PIN) ---
class PerfilUsuario(models.Model):
    usuario = models.OneToOneField(User, on_delete=models.CASCADE)
    pin = models.CharField(max_length=6, help_text='Pin de 4-6 digitos para acceso POS')
    rol = models.CharField(
        max_length=20,
        choices=[('ADMIN', 'Administrador'), ('CAJERO', 'Cajero'), ('COCINA', 'Cocina')],
        default='CAJERO',
    )

    def __str__(self):
        return f'{self.usuario.username} - {self.rol}'


# --- GESTION DE CLIENTES ---
class Cliente(models.Model):
    cedula_ruc = models.CharField(max_length=13, unique=True)
    nombre = models.CharField(max_length=200)
    direccion = models.TextField(blank=True)
    telefono = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.nombre} ({self.cedula_ruc})'


# --- GESTION DE PRODUCTOS ---
class Categoria(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='categories')
    nombre = models.CharField(max_length=50)
    icono = models.CharField(max_length=50, blank=True)

    def save(self, *args, **kwargs):
        if not self.organization_id:
            self.organization = Organization.get_or_create_default()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.nombre


class Producto(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='products')
    categoria = models.ForeignKey(Categoria, on_delete=models.CASCADE)
    nombre = models.CharField(max_length=100)
    precio = models.DecimalField(max_digits=10, decimal_places=2)
    imagen = models.ImageField(upload_to='productos/', null=True, blank=True)
    activo = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        if self.categoria_id and not self.organization_id:
            self.organization = self.categoria.organization
        if not self.organization_id:
            self.organization = Organization.get_or_create_default()
        if self.categoria_id and self.categoria.organization_id != self.organization_id:
            raise ValidationError('El producto no puede pertenecer a una organizacion distinta a la de su categoria.')
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.nombre} ($ {self.precio})'


class LocationInventory(models.Model):
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='inventory_items')
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='location_inventory_items')
    stock_actual = models.IntegerField(default=0)
    stock_minimo = models.IntegerField(default=5, help_text='Alerta cuando baje de esta cantidad')
    unidad = models.CharField(max_length=20, default='unidades', help_text='Ej: unidades, libras, cajas')
    ultima_actualizacion = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['location', 'producto'], name='uq_location_inventory_product'),
        ]

    @property
    def alerta_bajo(self):
        return self.stock_actual <= self.stock_minimo

    def __str__(self):
        return f'{self.location.name} / {self.producto.nombre}: {self.stock_actual} {self.unidad}'


# --- GESTION DE CAJA (TURNOS) ---
class CajaTurno(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.PROTECT)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, null=True, blank=True, related_name='cash_turns'
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name='cash_turns'
    )
    operator_opened_by = models.ForeignKey(
        StaffProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='opened_cash_turns',
    )
    operator_closed_by = models.ForeignKey(
        StaffProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='closed_cash_turns',
    )
    operating_day = models.DateField(null=True, blank=True, db_index=True)
    operating_day_ends_at_snapshot = models.TimeField(null=True, blank=True)
    timezone_snapshot = models.CharField(max_length=64, blank=True)

    fecha_apertura = models.DateTimeField(auto_now_add=True)
    fecha_cierre = models.DateTimeField(null=True, blank=True)
    base_inicial = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    monto_final_declarado = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    conteo_billetes = models.JSONField(default=dict, blank=True)
    total_efectivo_sistema = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_transferencia_sistema = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_otros_sistema = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    diferencia = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def _apply_tenant_defaults(self):
        if self.location_id and not self.organization_id:
            self.organization = self.location.organization
        if not self.location_id:
            self.location = Location.get_or_create_default()
        if self.location_id and not self.organization_id:
            self.organization = self.location.organization
        if self.location_id:
            if self.location.organization_id != self.organization_id:
                raise ValidationError('La caja no puede apuntar a una sucursal de otra organizacion.')
            if not self.timezone_snapshot:
                self.timezone_snapshot = self.location.timezone
            if not self.operating_day_ends_at_snapshot:
                self.operating_day_ends_at_snapshot = self.location.operating_day_ends_at
        if not self.timezone_snapshot:
            self.timezone_snapshot = DEFAULT_LOCATION_TIMEZONE
        if not self.operating_day_ends_at_snapshot:
            self.operating_day_ends_at_snapshot = DEFAULT_OPERATING_DAY_ENDS_AT
        if not self.operating_day:
            self.operating_day = compute_operating_day(
                timestamp=self.fecha_apertura or timezone.now(),
                timezone_name=self.timezone_snapshot,
                operating_day_ends_at=self.operating_day_ends_at_snapshot,
            )

    def save(self, *args, **kwargs):
        self._apply_tenant_defaults()
        super().save(*args, **kwargs)

    def cerrar_caja(self, monto_efectivo_real, conteo_json):
        from django.db.models import Sum

        self.fecha_cierre = timezone.now()
        self.monto_final_declarado = monto_efectivo_real
        self.conteo_billetes = conteo_json

        movimientos = self.movimientos.exclude(concepto='VENTA')
        total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or 0
        total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or 0

        esperado = self.base_inicial + self.total_efectivo_sistema + total_ingresos - total_egresos
        self.diferencia = monto_efectivo_real - esperado
        self.save()


# --- PEDIDOS (UNIFICADO WEB + POS) ---
class Venta(models.Model):
    class PaymentStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pendiente'
        PAID = 'PAID', 'Pagado'
        FAILED = 'FAILED', 'Fallido'
        VOIDED = 'VOIDED', 'Anulado'

    METODOS = [('EFECTIVO', 'Efectivo'), ('TRANSFERENCIA', 'Transferencia'), ('TARJETA', 'Tarjeta/Medianet')]
    ESTADOS_PAGO = [
        ('PENDIENTE', 'Pendiente'),
        ('APROBADO', 'Aprobado'),
        ('RECHAZADO', 'Rechazado'),
        ('ANULADO', 'Anulado'),
    ]
    ORIGEN = [('POS', 'Local'), ('WEB', 'Web App')]
    ESTADO = [
        ('PENDIENTE', 'Por Confirmar'),
        ('PENDIENTE_COTIZACION', 'Esperando Costo Envio'),
        ('COCINA', 'En Cocina'),
        ('LISTO', 'Listo/Entregado'),
        ('EN_CAMINO', 'En Camino'),
        ('CANCELADO', 'Cancelado'),
    ]
    TIPO_PEDIDO = [('SERVIR', 'Para Servir'), ('LLEVAR', 'Para Llevar'), ('DOMICILIO', 'A Domicilio')]
    CONFIRMACION_CLIENTE = [
        ('PENDIENTE', 'Pendiente'),
        ('ACEPTADA', 'Aceptada'),
        ('RECHAZADA', 'Rechazada'),
        ('EXPIRADA', 'Expirada'),
    ]

    turno = models.ForeignKey(CajaTurno, related_name='ventas', on_delete=models.PROTECT, null=True)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, null=True, blank=True, related_name='sales'
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name='sales'
    )
    operator = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='operated_sales'
    )
    supervisor = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='supervised_sales'
    )
    operator_display_name_snapshot = models.CharField(max_length=200, blank=True)
    supervisor_display_name_snapshot = models.CharField(max_length=200, blank=True)
    operating_day = models.DateField(null=True, blank=True, db_index=True)
    client_transaction_id = models.CharField(max_length=64, blank=True, db_index=True)

    cliente = models.ForeignKey(Cliente, on_delete=models.SET_NULL, null=True, blank=True)
    fecha = models.DateTimeField(auto_now_add=True)
    origen = models.CharField(max_length=10, choices=ORIGEN, default='POS')
    tipo_pedido = models.CharField(max_length=20, choices=TIPO_PEDIDO, default='SERVIR')
    cliente_nombre = models.CharField(max_length=100, default='CONSUMIDOR FINAL')

    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    metodo_pago = models.CharField(max_length=20, choices=METODOS)
    referencia_pago = models.CharField(max_length=40, blank=True, help_text='Referencia/Lote/Aprobacion de pago')
    tarjeta_tipo = models.CharField(max_length=12, blank=True, help_text='CREDITO o DEBITO')
    tarjeta_marca = models.CharField(max_length=20, blank=True, help_text='VISA, MASTERCARD, etc.')
    estado_pago = models.CharField(max_length=12, choices=ESTADOS_PAGO, default='APROBADO')
    payment_status = models.CharField(
        max_length=12,
        choices=PaymentStatus.choices,
        blank=True,
        default='',
        db_index=True,
    )
    payment_method_type = models.CharField(max_length=20, blank=True)
    payment_reference = models.CharField(max_length=80, blank=True)
    payment_provider = models.CharField(max_length=50, blank=True)
    payment_failure_reason = models.CharField(max_length=255, blank=True)
    payment_checked_at = models.DateTimeField(null=True, blank=True)
    monto_recibido = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    costo_envio = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    comprobante_foto = models.ImageField(upload_to='pagos/', null=True, blank=True)
    estado = models.CharField(max_length=25, choices=ESTADO, default='PENDIENTE')

    direccion_envio = models.TextField(blank=True, help_text='Direccion texto libre para domicilios')
    telefono_cliente = models.CharField(max_length=20, blank=True, help_text='Telefono del cliente para contacto')
    email_cliente = models.EmailField(blank=True, help_text='Correo del cliente para enviar comprobante final')
    ubicacion_lat = models.FloatField(null=True, blank=True, help_text='Latitud GPS del cliente')
    ubicacion_lng = models.FloatField(null=True, blank=True, help_text='Longitud GPS del cliente')
    tiempo_estimado_minutos = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Tiempo estimado restante informado por el repartidor cuando el pedido va en camino.',
    )
    salio_a_reparto_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Momento en que el repartidor marco el pedido como EN_CAMINO.',
    )
    cliente_reporto_recibido_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Momento en que el cliente reporto haber recibido el pedido.',
    )
    repartidor_confirmo_entrega_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Momento en que el repartidor confirmo la entrega final.',
    )

    telefono_cliente_e164 = models.CharField(max_length=20, blank=True, db_index=True)
    confirmacion_cliente = models.CharField(max_length=12, choices=CONFIRMACION_CLIENTE, default='PENDIENTE')
    confirmada_por_bot_at = models.DateTimeField(null=True, blank=True)
    delivery_quote_deadline_at = models.DateTimeField(null=True, blank=True)
    repartidor_asignado = models.ForeignKey(
        'Empleado', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pedidos_asignados', help_text='Repartidor que tomo el pedido',
    )

    def _resolve_authoritative_payment_status(self) -> str:
        status = str(self.payment_status or '').strip().upper()
        if status:
            if status not in self.PaymentStatus.values:
                raise ValidationError('payment_status invalido para la venta.')
            return status
        legacy_status = str(self.estado_pago or '').strip().upper()
        return LEGACY_TO_V2_PAYMENT_STATUS.get(legacy_status, self.PaymentStatus.PAID)

    def _sync_payment_compatibility_fields(self):
        # payment_status is the canonical field. Legacy fields are kept in sync
        # only for compatibility with older reads and prints.
        self.payment_status = self._resolve_authoritative_payment_status()
        self.estado_pago = V2_TO_LEGACY_PAYMENT_STATUS.get(self.payment_status, self.estado_pago)

        payment_method_type = str(self.payment_method_type or self.metodo_pago or '').strip().upper()
        if payment_method_type:
            self.payment_method_type = payment_method_type
            if payment_method_type in dict(self.METODOS):
                self.metodo_pago = payment_method_type

        payment_reference = str(self.payment_reference or self.referencia_pago or '').strip()
        if payment_reference:
            self.payment_reference = payment_reference[:80]
            self.referencia_pago = payment_reference[:40]

    def _apply_tenant_defaults(self):
        if self.turno_id:
            if not self.location_id and self.turno.location_id:
                self.location = self.turno.location
            if not self.organization_id and self.turno.organization_id:
                self.organization = self.turno.organization
            if not self.operating_day and self.turno.operating_day:
                self.operating_day = self.turno.operating_day
        if self.location_id and not self.organization_id:
            self.organization = self.location.organization
        if not self.location_id and self.turno_id is None:
            self.location = Location.get_or_create_default()
        if self.location_id and not self.organization_id:
            self.organization = self.location.organization
        if self.location_id and self.organization_id and self.location.organization_id != self.organization_id:
            raise ValidationError('La venta no puede pertenecer a una organizacion distinta a la de la sucursal.')
        if self.turno_id and self.location_id and self.turno.location_id and self.turno.location_id != self.location_id:
            raise ValidationError('La venta no puede apuntar a una sucursal distinta al turno.')
        if self.turno_id and self.organization_id and self.turno.organization_id and self.turno.organization_id != self.organization_id:
            raise ValidationError('La venta no puede apuntar a una organizacion distinta al turno.')
        if not self.operating_day:
            if self.turno_id and self.turno.operating_day:
                self.operating_day = self.turno.operating_day
            elif self.location_id:
                self.operating_day = compute_operating_day(
                    timestamp=self.fecha or timezone.now(),
                    timezone_name=self.location.timezone,
                    operating_day_ends_at=self.location.operating_day_ends_at,
                )
        self._sync_payment_compatibility_fields()
        if self.operator_id and not self.operator_display_name_snapshot:
            self.operator_display_name_snapshot = self.operator.display_name
        if self.supervisor_id and not self.supervisor_display_name_snapshot:
            self.supervisor_display_name_snapshot = self.supervisor.display_name

    def save(self, *args, **kwargs):
        self._apply_tenant_defaults()
        super().save(*args, **kwargs)

    @property
    def total_con_envio(self):
        total_base = self.total or Decimal('0.00')
        envio = self.costo_envio or Decimal('0.00')
        return total_base + envio

    @property
    def cambio(self):
        if self.monto_recibido is not None:
            return max(self.monto_recibido - self.total_con_envio, Decimal('0.00'))
        return Decimal('0.00')

    @property
    def minutos_restantes_estimados(self):
        if not self.tiempo_estimado_minutos:
            return None
        if not self.salio_a_reparto_at:
            return self.tiempo_estimado_minutos

        elapsed_seconds = max((timezone.now() - self.salio_a_reparto_at).total_seconds(), 0)
        elapsed_minutes = int(elapsed_seconds // 60)
        return max(self.tiempo_estimado_minutos - elapsed_minutes, 0)


class DetalleVenta(models.Model):
    venta = models.ForeignKey(Venta, related_name='detalles', on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT)
    cantidad = models.PositiveIntegerField(default=1)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    precio_bruto_unitario = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    descuento_monto = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    impuesto_monto = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal_neto = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pricing_rule_snapshot = models.JSONField(default=dict, blank=True)
    tax_rule_snapshot = models.JSONField(default=dict, blank=True)
    discount_rule_snapshot = models.JSONField(default=dict, blank=True)
    nota = models.CharField(max_length=200, blank=True)

    def save(self, *args, **kwargs):
        if not self.precio_bruto_unitario:
            self.precio_bruto_unitario = self.precio_unitario
        if not self.subtotal_neto:
            bruto = self.precio_bruto_unitario * self.cantidad
            self.subtotal_neto = bruto - self.descuento_monto + self.impuesto_monto
        super().save(*args, **kwargs)

    @property
    def subtotal(self):
        return self.subtotal_neto or (self.cantidad * self.precio_unitario)


# --- GESTION DE EMPLEADOS Y ASISTENCIA LEGACY ---
class Empleado(models.Model):
    ROLES = [
        ('ADMIN', 'Administrador'),
        ('CAJERO', 'Cajero'),
        ('COCINA', 'Cocina'),
        ('MESERO', 'Mesero'),
        ('DELIVERY', 'Delivery'),
        ('OTRO', 'Otro'),
    ]

    nombre = models.CharField(max_length=200)
    cedula = models.CharField(max_length=13, unique=True, null=True, blank=True)
    telefono = models.CharField(max_length=20, blank=True)
    direccion = models.TextField(blank=True)
    pin = models.CharField(max_length=4, unique=True, help_text='PIN de 4 digitos')
    rol = models.CharField(max_length=20, choices=ROLES, default='OTRO')
    activo = models.BooleanField(default=True)
    usuario = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='empleado')
    fecha_registro = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.nombre} ({self.rol})'


class Asistencia(models.Model):
    empleado = models.ForeignKey(Empleado, on_delete=models.CASCADE, related_name='asistencias', null=True)
    fecha = models.DateField(auto_now_add=True)
    hora_entrada = models.TimeField(auto_now_add=True)
    hora_salida = models.TimeField(null=True, blank=True)

    def registrar_salida(self):
        self.hora_salida = timezone.localtime().time()
        self.save()


# --- MOVIMIENTOS DE CAJA (INGRESOS/GASTOS) ---
class MovimientoCaja(models.Model):
    TIPOS = [('INGRESO', 'Ingreso'), ('EGRESO', 'Egreso')]
    CONCEPTO_REEMBOLSO_HEREDADO = 'REEMBOLSO_HEREDADO'
    CONCEPTOS_EGRESO = [
        ('ALMUERZO', 'Almuerzo Empleados'),
        ('GAS', 'Gas / Combustible'),
        ('COMPRAS', 'Compras / Insumos'),
        ('TRANSPORTE', 'Transporte / Delivery'),
        ('MANTENIMIENTO', 'Mantenimiento'),
        ('SERVICIOS', 'Servicios (Agua/Luz/Internet)'),
        ('OTRO_EGRESO', 'Otro Egreso'),
    ]
    CONCEPTOS_INGRESO = [
        ('PROPINA', 'Propina'),
        ('DEVOLUCION', 'Devolucion Proveedor'),
        ('OTRO_INGRESO', 'Otro Ingreso'),
    ]

    turno = models.ForeignKey(CajaTurno, related_name='movimientos', on_delete=models.PROTECT)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, null=True, blank=True, related_name='cash_movements'
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name='cash_movements'
    )
    operator = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='cash_movements'
    )
    supervisor = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='authorized_cash_movements'
    )
    authorization_reason_code = models.CharField(max_length=50, blank=True)
    authorization_reason_note = models.CharField(max_length=255, blank=True)
    tipo = models.CharField(max_length=10, choices=TIPOS)
    concepto = models.CharField(max_length=30)
    descripcion = models.CharField(max_length=200, blank=True, help_text='Detalle libre: ej. Almuerzo 3 empleados')
    monto = models.DecimalField(max_digits=10, decimal_places=2)
    fecha = models.DateTimeField(auto_now_add=True)
    registrado_por = models.ForeignKey(User, on_delete=models.PROTECT, null=True)

    class Meta:
        ordering = ['-fecha']

    def save(self, *args, **kwargs):
        if self.turno_id:
            if not self.location_id and self.turno.location_id:
                self.location = self.turno.location
            if not self.organization_id and self.turno.organization_id:
                self.organization = self.turno.organization
        if self.location_id and not self.organization_id:
            self.organization = self.location.organization
        if self.location_id and self.organization_id and self.location.organization_id != self.organization_id:
            raise ValidationError('El movimiento de caja no puede pertenecer a otra organizacion.')
        super().save(*args, **kwargs)

    def __str__(self):
        signo = '+' if self.tipo == 'INGRESO' else '-'
        return f"{signo}${self.monto} - {self.concepto} ({self.fecha.strftime('%d/%m %H:%M')})"


# --- CONTROL DE INVENTARIO LEGACY ---
class Inventario(models.Model):
    producto = models.OneToOneField(Producto, on_delete=models.CASCADE, related_name='inventario')
    stock_actual = models.IntegerField(default=0)
    stock_minimo = models.IntegerField(default=5, help_text='Alerta cuando baje de esta cantidad')
    unidad = models.CharField(max_length=20, default='unidades', help_text='Ej: unidades, libras, cajas')
    ultima_actualizacion = models.DateTimeField(auto_now=True)

    @property
    def alerta_bajo(self):
        return self.stock_actual <= self.stock_minimo

    def __str__(self):
        return f'{self.producto.nombre}: {self.stock_actual} {self.unidad}'


class MovimientoInventario(models.Model):
    TIPOS = [
        ('ENTRADA', 'Entrada / Compra'),
        ('SALIDA', 'Salida / Venta'),
        ('AJUSTE', 'Ajuste Manual'),
        ('MERMA', 'Merma / Desperdicio'),
    ]

    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='movimientos_inv')
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name='inventory_movements'
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, null=True, blank=True, related_name='inventory_movements'
    )
    venta = models.ForeignKey('Venta', on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_movements')
    tipo = models.CharField(max_length=10, choices=TIPOS)
    cantidad = models.IntegerField(help_text='Cantidad (+entrada, -salida)')
    stock_anterior = models.IntegerField()
    stock_nuevo = models.IntegerField()
    concepto = models.CharField(max_length=200, blank=True)
    fecha = models.DateTimeField(auto_now_add=True)
    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    class Meta:
        ordering = ['-fecha']

    def save(self, *args, **kwargs):
        if self.location_id and not self.organization_id:
            self.organization = self.location.organization
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.producto.nombre}: {self.tipo} {self.cantidad} ({self.fecha.strftime('%d/%m %H:%M')})"


class DeliveryQuote(models.Model):
    ESTADOS = [
        ('PROPUESTA', 'Propuesta'),
        ('GANADORA', 'Ganadora'),
        ('DESCARTADA', 'Descartada'),
    ]

    venta = models.ForeignKey(Venta, on_delete=models.CASCADE, related_name='delivery_quotes')
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, null=True, blank=True, related_name='delivery_quotes'
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name='delivery_quotes'
    )
    empleado_delivery = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='cotizaciones_delivery')
    precio = models.DecimalField(max_digits=10, decimal_places=2)
    estado = models.CharField(max_length=12, choices=ESTADOS, default='PROPUESTA')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def save(self, *args, **kwargs):
        if self.venta_id:
            if not self.location_id and self.venta.location_id:
                self.location = self.venta.location
            if not self.organization_id and self.venta.organization_id:
                self.organization = self.venta.organization
        super().save(*args, **kwargs)


class WhatsAppConversation(models.Model):
    ESTADOS = [
        ('NUEVO', 'Nuevo'),
        ('LINK_ENVIADO', 'Link Enviado'),
        ('ESPERANDO_CONFIRMACION_TOTAL', 'Esperando Confirmacion Total'),
        ('FINALIZADO', 'Finalizado'),
    ]

    telefono_e164 = models.CharField(max_length=20, unique=True)
    estado_flujo = models.CharField(max_length=40, choices=ESTADOS, default='NUEVO')
    venta = models.ForeignKey(Venta, on_delete=models.SET_NULL, null=True, blank=True, related_name='conversaciones_whatsapp')
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_outbound_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.telefono_e164} [{self.estado_flujo}]'


class WhatsAppMessageLog(models.Model):
    DIRECCIONES = [('IN', 'Inbound'), ('OUT', 'Outbound')]

    direction = models.CharField(max_length=3, choices=DIRECCIONES)
    telefono_e164 = models.CharField(max_length=20, db_index=True)
    message_sid = models.CharField(max_length=64, null=True, blank=True, unique=True)
    payload_json = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=32, default='queued')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class PrintJob(models.Model):
    TIPOS = [('COMANDA', 'Comanda'), ('TICKET', 'Ticket')]
    ESTADOS = [
        ('PENDING', 'Pending'),
        ('IN_PROGRESS', 'In Progress'),
        ('DONE', 'Done'),
        ('FAILED', 'Failed'),
    ]

    venta = models.ForeignKey(Venta, on_delete=models.CASCADE, related_name='print_jobs')
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, null=True, blank=True, related_name='print_jobs'
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name='print_jobs'
    )
    tipo = models.CharField(max_length=10, choices=TIPOS)
    estado = models.CharField(max_length=12, choices=ESTADOS, default='PENDING')
    reintentos = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=255, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(fields=['venta', 'tipo'], name='uq_printjob_sale_type'),
        ]

    def save(self, *args, **kwargs):
        if self.venta_id:
            if not self.location_id and self.venta.location_id:
                self.location = self.venta.location
            if not self.organization_id and self.venta.organization_id:
                self.organization = self.venta.organization
        super().save(*args, **kwargs)


class IdempotencyRecord(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED_RETRYABLE = 'FAILED_RETRYABLE', 'Failed Retryable'
        FAILED_FINAL = 'FAILED_FINAL', 'Failed Final'

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='idempotency_records')
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='idempotency_records')
    client_transaction_id = models.CharField(max_length=64)
    request_fingerprint = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    venta = models.ForeignKey(Venta, on_delete=models.SET_NULL, null=True, blank=True, related_name='idempotency_records')
    response_payload = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['location', 'client_transaction_id'],
                name='uq_idempotency_location_client_transaction',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'expires_at']),
        ]

    def __str__(self):
        return f'{self.location.name}:{self.client_transaction_id} ({self.status})'


class SupervisorAuthorization(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='supervisor_authorizations')
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='supervisor_authorizations')
    operator = models.ForeignKey(StaffProfile, on_delete=models.CASCADE, related_name='requested_authorizations')
    supervisor = models.ForeignKey(StaffProfile, on_delete=models.CASCADE, related_name='granted_authorizations')
    cart_fingerprint = models.CharField(max_length=128)
    reason_code = models.CharField(max_length=50)
    reason_note = models.CharField(max_length=255, blank=True)
    authorized_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-authorized_at']


class OutboxEvent(models.Model):
    class Priority(models.IntegerChoices):
        CRITICAL = 10, 'Critical'
        HIGH = 20, 'High'
        NORMAL = 30, 'Normal'
        LOW = 40, 'Low'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
        DONE = 'DONE', 'Done'
        FAILED = 'FAILED', 'Failed'
        BLOCKED = 'BLOCKED', 'Blocked'

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='outbox_events')
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='outbox_events')
    aggregate_type = models.CharField(max_length=80)
    aggregate_id = models.CharField(max_length=64)
    event_type = models.CharField(max_length=80)
    payload_json = models.JSONField(default=dict, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    priority = models.PositiveSmallIntegerField(
        choices=Priority.choices,
        default=Priority.NORMAL,
        db_index=True,
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['priority', 'created_at']
        indexes = [
            models.Index(fields=['status', 'priority', 'available_at']),
        ]


class AuditLog(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='audit_logs')
    location = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    actor_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    actor_staff = models.ForeignKey(StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    event_type = models.CharField(max_length=80)
    target_model = models.CharField(max_length=80)
    target_id = models.CharField(max_length=64)
    payload_json = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    requires_attention = models.BooleanField(default=False, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='resolved_audit_logs',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class LedgerRegistryActivation(models.Model):
    singleton_key = models.CharField(max_length=20, unique=True, default='default', editable=False)
    active_registry_version = models.CharField(max_length=64)
    active_registry_hash = models.CharField(max_length=64)
    min_supported_queue_schema = models.PositiveIntegerField(default=MIN_SUPPORTED_QUEUE_SCHEMA)
    maintenance_mode = models.BooleanField(default=False)
    activated_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Ledger registry activation'
        verbose_name_plural = 'Ledger registry activation'

    @classmethod
    def get_solo(cls):
        activation, _created = cls.objects.get_or_create(
            singleton_key='default',
            defaults={
                'active_registry_version': REGISTRY_VERSION,
                'active_registry_hash': get_registry_hash(),
                'min_supported_queue_schema': MIN_SUPPORTED_QUEUE_SCHEMA,
                'maintenance_mode': False,
            },
        )
        return activation

    def __str__(self):
        return f'{self.active_registry_version} ({self.active_registry_hash[:12]})'


class LedgerAccount(models.Model):
    class AccountType(models.TextChoices):
        ASSET = 'ASSET', 'Activo'
        LIABILITY = 'LIABILITY', 'Pasivo'
        INCOME = 'INCOME', 'Ingreso'
        EXPENSE = 'EXPENSE', 'Gasto'
        EQUITY = 'EQUITY', 'Patrimonio'

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='ledger_accounts'
    )
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    account_type = models.CharField(max_length=16, choices=AccountType.choices)
    system_code = models.CharField(max_length=40, null=True, blank=True, db_index=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['code', 'name']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'code'], name='uq_ledger_account_org_code'),
            models.UniqueConstraint(
                fields=['organization', 'system_code'],
                condition=Q(system_code__isnull=False),
                name='uq_ledger_account_org_system_code',
            ),
        ]

    def clean(self):
        if self.system_code:
            defaults = SYSTEM_LEDGER_ACCOUNT_DEFAULTS.get(self.system_code)
            if not defaults:
                raise ValidationError({'system_code': 'Codigo de cuenta de sistema desconocido.'})

            for field_name in ('code', 'name', 'account_type'):
                current_value = getattr(self, field_name)
                expected_value = defaults[field_name]
                if current_value != expected_value:
                    raise ValidationError(
                        {field_name: f'La cuenta de sistema {self.system_code} debe conservar {field_name}={expected_value}.'}
                    )

        if not self.pk:
            return

        previous = LedgerAccount.objects.filter(pk=self.pk).only(
            'system_code',
            'organization_id',
            'code',
            'name',
            'account_type',
            'active',
        ).first()
        if previous and previous.system_code:
            immutable_fields = ('organization_id', 'system_code', 'code', 'name', 'account_type', 'active')
            for field_name in immutable_fields:
                if getattr(previous, field_name) != getattr(self, field_name):
                    raise ValidationError(
                        f'La cuenta de sistema {previous.system_code} es inmutable y no puede modificarse.'
                    )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.system_code:
            raise ValidationError(f'La cuenta de sistema {self.system_code} no puede eliminarse.')
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f'{self.code} - {self.name}'


class AccountingAdjustment(models.Model):
    class AdjustmentType(models.TextChoices):
        ORPHAN_PAYMENT_UNIDENTIFIED = 'ORPHAN_PAYMENT_UNIDENTIFIED', 'Pago huerfano por identificar'
        ORPHAN_PAYMENT_REFUND_PENDING = 'ORPHAN_PAYMENT_REFUND_PENDING', 'Pago huerfano con reembolso pendiente'

    class AccountBucket(models.TextChoices):
        PENDING_IDENTIFICATION = 'PENDING_IDENTIFICATION', 'Pendientes por identificar'
        REFUND_LIABILITY = 'REFUND_LIABILITY', 'Reembolsos pendientes'

    class SystemLedgerCode(models.TextChoices):
        PAYMENT_GATEWAY_CLEARING = 'PAYMENT_GATEWAY_CLEARING', 'Cobros pasarela / banco'
        UNIDENTIFIED_RECEIPTS = 'UNIDENTIFIED_RECEIPTS', 'Ingresos por identificar'
        REFUND_PAYABLE = 'REFUND_PAYABLE', 'Reembolsos pendientes'

    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        RESOLVED = 'RESOLVED', 'Resolved'

    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name='accounting_adjustments'
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name='accounting_adjustments'
    )
    sale = models.ForeignKey(
        Venta, on_delete=models.PROTECT, null=True, blank=True, related_name='accounting_adjustments'
    )
    source_audit_log = models.OneToOneField(
        AuditLog, on_delete=models.PROTECT, related_name='accounting_adjustment'
    )
    adjustment_type = models.CharField(max_length=40, choices=AdjustmentType.choices)
    account_bucket = models.CharField(max_length=40, choices=AccountBucket.choices)
    source_account = models.ForeignKey(
        LedgerAccount,
        on_delete=models.PROTECT,
        related_name='source_accounting_adjustments',
    )
    destination_account = models.ForeignKey(
        LedgerAccount,
        on_delete=models.PROTECT,
        related_name='destination_accounting_adjustments',
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    operating_day = models.DateField(null=True, blank=True, db_index=True)
    effective_at = models.DateTimeField(default=timezone.now, db_index=True)
    payment_reference = models.CharField(max_length=80, blank=True)
    payment_provider = models.CharField(max_length=50, blank=True)
    external_reference = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=255, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_accounting_adjustments',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-effective_at', '-created_at']

    def get_source_account_display(self):
        return self.source_account.name if self.source_account_id else ''

    def get_destination_account_display(self):
        return self.destination_account.name if self.destination_account_id else ''


SYSTEM_LEDGER_ACCOUNT_DEFAULTS = {
    system_code: {
        **defaults,
        'account_type': defaults['account_type'],
    }
    for system_code, defaults in get_system_account_defaults_map().items()
}


def ensure_system_ledger_account(*, organization: Organization, system_code: str) -> LedgerAccount:
    defaults = SYSTEM_LEDGER_ACCOUNT_DEFAULTS.get(system_code)
    if not defaults:
        raise ValidationError(f'No existe configuracion sistema para la cuenta {system_code}')

    account, created = LedgerAccount.objects.get_or_create(
        organization=organization,
        system_code=system_code,
        defaults={
            'code': defaults['code'],
            'name': defaults['name'],
            'account_type': defaults['account_type'],
            'active': True,
        },
    )
    if not created:
        for field_name in ('code', 'name', 'account_type'):
            if getattr(account, field_name) != defaults[field_name]:
                raise ValidationError(
                    f'La cuenta de sistema {system_code} no coincide con el registry actual.'
                )
        if not account.active:
            raise ValidationError(f'La cuenta de sistema {system_code} esta inactiva y debe corregirse manualmente.')
    return account


def provision_system_ledger_accounts(*, organization: Organization) -> dict[str, list[str]]:
    created_system_codes: list[str] = []
    validated_system_codes: list[str] = []
    for account_definition in get_system_account_definitions():
        system_code = account_definition['system_code']
        exists = LedgerAccount.objects.filter(
            organization=organization,
            system_code=system_code,
        ).exists()
        ensure_system_ledger_account(organization=organization, system_code=system_code)
        if exists:
            validated_system_codes.append(system_code)
        else:
            created_system_codes.append(system_code)

    return {
        'created_system_codes': created_system_codes,
        'validated_system_codes': validated_system_codes,
    }
