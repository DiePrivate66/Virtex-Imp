from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

# --- PERFIL DE USUARIO (PIN) ---
class PerfilUsuario(models.Model):
    usuario = models.OneToOneField(User, on_delete=models.CASCADE)
    pin = models.CharField(max_length=6, help_text="Pin de 4-6 dígitos para acceso POS")
    rol = models.CharField(max_length=20, choices=[('ADMIN', 'Administrador'), ('CAJERO', 'Cajero'), ('COCINA', 'Cocina')], default='CAJERO')

    def __str__(self): return f"{self.usuario.username} - {self.rol}"

# --- GESTIÓN DE CLIENTES ---
class Cliente(models.Model):
    cedula_ruc = models.CharField(max_length=13, unique=True)
    nombre = models.CharField(max_length=200)
    direccion = models.TextField(blank=True)
    telefono = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)

    def __str__(self): return f"{self.nombre} ({self.cedula_ruc})"

# --- GESTIÓN DE PRODUCTOS ---
class Categoria(models.Model):
    nombre = models.CharField(max_length=50)
    icono = models.CharField(max_length=50, blank=True)
    
    def __str__(self): return self.nombre

class Producto(models.Model):
    categoria = models.ForeignKey(Categoria, on_delete=models.CASCADE)
    nombre = models.CharField(max_length=100)
    precio = models.DecimalField(max_digits=10, decimal_places=2)
    imagen = models.ImageField(upload_to='productos/', null=True, blank=True)
    activo = models.BooleanField(default=True)
    
    def __str__(self): return f"{self.nombre} ($ {self.precio})"

# --- GESTIÓN DE CAJA (TURNOS) ---
class CajaTurno(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.PROTECT)
    fecha_apertura = models.DateTimeField(auto_now_add=True)
    fecha_cierre = models.DateTimeField(null=True, blank=True)
    base_inicial = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Valores declarados por el cajero al cierre
    monto_final_declarado = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    conteo_billetes = models.JSONField(default=dict, blank=True) # Guardará {"20": 5, "10": 2, ...}
    
    # Totales calculados por sistema
    total_efectivo_sistema = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_transferencia_sistema = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_otros_sistema = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Diferencia (Sobrante/Faltante)
    diferencia = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    def cerrar_caja(self, monto_efectivo_real, conteo_json):
        from django.db.models import Sum
        self.fecha_cierre = timezone.now()
        self.monto_final_declarado = monto_efectivo_real
        self.conteo_billetes = conteo_json
        
        # Calcular egresos e ingresos extra del turno
        movimientos = self.movimientos.all()
        total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or 0
        total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or 0
        
        # Esperado = Base + Ventas Efectivo + Ingresos Extra - Egresos
        esperado = self.base_inicial + self.total_efectivo_sistema + total_ingresos - total_egresos
        self.diferencia = monto_efectivo_real - esperado
        
        self.save()

# --- PEDIDOS (UNIFICADO WEB + POS) ---
class Venta(models.Model):
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
        ('PENDIENTE_COTIZACION', 'Esperando Costo Envío'),
        ('COCINA', 'En Cocina'),
        ('LISTO', 'Listo/Entregado'),
        ('EN_CAMINO', 'En Camino'),
        ('CANCELADO', 'Cancelado'),
    ]
    TIPO_PEDIDO = [('SERVIR', 'Para Servir'), ('LLEVAR', 'Para Llevar'), ('DOMICILIO', 'A Domicilio')]

    turno = models.ForeignKey(CajaTurno, related_name='ventas', on_delete=models.PROTECT, null=True)
    cliente = models.ForeignKey(Cliente, on_delete=models.SET_NULL, null=True, blank=True)
    fecha = models.DateTimeField(auto_now_add=True)
    origen = models.CharField(max_length=10, choices=ORIGEN, default='POS')
    tipo_pedido = models.CharField(max_length=20, choices=TIPO_PEDIDO, default='SERVIR')
    
    # Campo legacy por si no se registra cliente, pero idealmente usamos la FK
    cliente_nombre = models.CharField(max_length=100, default="CONSUMIDOR FINAL")
    
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    metodo_pago = models.CharField(max_length=20, choices=METODOS)
    referencia_pago = models.CharField(max_length=40, blank=True, help_text='Referencia/Lote/Aprobación de pago')
    tarjeta_tipo = models.CharField(max_length=12, blank=True, help_text='CREDITO o DEBITO')
    tarjeta_marca = models.CharField(max_length=20, blank=True, help_text='VISA, MASTERCARD, etc.')
    estado_pago = models.CharField(max_length=12, choices=ESTADOS_PAGO, default='APROBADO')
    monto_recibido = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    costo_envio = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    comprobante_foto = models.ImageField(upload_to='pagos/', null=True, blank=True)
    estado = models.CharField(max_length=25, choices=ESTADO, default='PENDIENTE')
    
    # Campos PWA Domicilio
    direccion_envio = models.TextField(blank=True, help_text='Dirección texto libre para domicilios')
    telefono_cliente = models.CharField(max_length=20, blank=True, help_text='Teléfono del cliente para contacto')
    ubicacion_lat = models.FloatField(null=True, blank=True, help_text='Latitud GPS del cliente')
    ubicacion_lng = models.FloatField(null=True, blank=True, help_text='Longitud GPS del cliente')

    @property
    def cambio(self):
        from decimal import Decimal
        if self.monto_recibido is not None and self.total is not None:
            return max(self.monto_recibido - self.total, Decimal('0.00'))
        return Decimal('0.00')

class DetalleVenta(models.Model):
    venta = models.ForeignKey(Venta, related_name='detalles', on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT)
    cantidad = models.PositiveIntegerField(default=1)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    nota = models.CharField(max_length=200, blank=True)

    @property
    def subtotal(self):
        return self.cantidad * self.precio_unitario 

# --- GESTIÓN DE EMPLEADOS Y ASISTENCIA ---
class Empleado(models.Model):
    ROLES = [
        ('ADMIN', 'Administrador'), 
        ('CAJERO', 'Cajero'), 
        ('COCINA', 'Cocina'), 
        ('MESERO', 'Mesero'),
        ('OTRO', 'Otro')
    ]
    
    nombre = models.CharField(max_length=200)
    cedula = models.CharField(max_length=13, unique=True, null=True, blank=True)
    telefono = models.CharField(max_length=20, blank=True)
    direccion = models.TextField(blank=True)
    pin = models.CharField(max_length=4, unique=True, help_text="PIN de 4 dígitos")
    rol = models.CharField(max_length=20, choices=ROLES, default='OTRO')
    activo = models.BooleanField(default=True)
    
    # Usuario de Django opcional (Solo para quienes necesitan loguearse en sistema: Admin/Cajero)
    usuario = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='empleado')
    
    fecha_registro = models.DateTimeField(auto_now_add=True)

    def __str__(self): return f"{self.nombre} ({self.rol})"

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
        ('DEVOLUCION', 'Devolución Proveedor'),
        ('OTRO_INGRESO', 'Otro Ingreso'),
    ]
    
    turno = models.ForeignKey(CajaTurno, related_name='movimientos', on_delete=models.PROTECT)
    tipo = models.CharField(max_length=10, choices=TIPOS)
    concepto = models.CharField(max_length=30)
    descripcion = models.CharField(max_length=200, blank=True, help_text='Detalle libre: ej. "Almuerzo 3 empleados"')
    monto = models.DecimalField(max_digits=10, decimal_places=2)
    fecha = models.DateTimeField(auto_now_add=True)
    registrado_por = models.ForeignKey(User, on_delete=models.PROTECT, null=True)
    
    class Meta:
        ordering = ['-fecha']
    
    def __str__(self):
        signo = '+' if self.tipo == 'INGRESO' else '-'
        return f"{signo}${self.monto} — {self.concepto} ({self.fecha.strftime('%d/%m %H:%M')})"

# --- CONTROL DE INVENTARIO ---
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
        return f"{self.producto.nombre}: {self.stock_actual} {self.unidad}"

class MovimientoInventario(models.Model):
    TIPOS = [
        ('ENTRADA', 'Entrada / Compra'),
        ('SALIDA', 'Salida / Venta'),
        ('AJUSTE', 'Ajuste Manual'),
        ('MERMA', 'Merma / Desperdicio'),
    ]
    
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='movimientos_inv')
    tipo = models.CharField(max_length=10, choices=TIPOS)
    cantidad = models.IntegerField(help_text='Cantidad (+entrada, -salida)')
    stock_anterior = models.IntegerField()
    stock_nuevo = models.IntegerField()
    concepto = models.CharField(max_length=200, blank=True)
    fecha = models.DateTimeField(auto_now_add=True)
    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    
    class Meta:
        ordering = ['-fecha']
    
    def __str__(self):
        return f"{self.producto.nombre}: {self.tipo} {self.cantidad} ({self.fecha.strftime('%d/%m %H:%M')})"
