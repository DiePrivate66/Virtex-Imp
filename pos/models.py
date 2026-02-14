from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

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
    monto_final_declarado = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    
    # Cálculos automáticos
    total_efectivo_sistema = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_transferencia_sistema = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    def cerrar_caja(self, monto_real):
        self.fecha_cierre = timezone.now()
        self.monto_final_declarado = monto_real
        self.save()

# --- PEDIDOS (UNIFICADO WEB + POS) ---
class Venta(models.Model):
    METODOS = [('EFECTIVO', 'Efectivo'), ('TRANSFERENCIA', 'Transferencia')]
    ORIGEN = [('POS', 'Local'), ('WEB', 'Web App')]
    ESTADO = [('PENDIENTE', 'Por Confirmar'), ('COCINA', 'En Cocina'), ('LISTO', 'Listo/Entregado')]

    turno = models.ForeignKey(CajaTurno, related_name='ventas', on_delete=models.PROTECT, null=True)
    fecha = models.DateTimeField(auto_now_add=True)
    origen = models.CharField(max_length=10, choices=ORIGEN, default='POS')
    cliente_nombre = models.CharField(max_length=100, default="Cliente Final")
    
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    metodo_pago = models.CharField(max_length=20, choices=METODOS)
    comprobante_foto = models.ImageField(upload_to='pagos/', null=True, blank=True)
    estado = models.CharField(max_length=20, choices=ESTADO, default='PENDIENTE')

class DetalleVenta(models.Model):
    venta = models.ForeignKey(Venta, related_name='detalles', on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT)
    cantidad = models.PositiveIntegerField(default=1)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    nota = models.CharField(max_length=200, blank=True)

    def subtotal(self):
        return self.cantidad * self.precio_unitario 
    


# ... (Tu código anterior de Venta y DetalleVenta) ...

# --- CONTROL DE ASISTENCIA ---
class Asistencia(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    fecha = models.DateField(auto_now_add=True)
    hora_entrada = models.TimeField(auto_now_add=True)
    hora_salida = models.TimeField(null=True, blank=True)
    horas_trabajadas = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    def marcar_salida(self):
        from datetime import datetime, date
        now = datetime.now().time()
        self.hora_salida = now
        # Cálculo simple de horas (opcional para MVP)
        self.save()

    def __str__(self):
        return f"{self.usuario.username} - {self.fecha}"