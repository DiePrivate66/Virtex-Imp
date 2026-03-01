import json
from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.utils import timezone
from django.db.models import Sum
from .models import CajaTurno, Asistencia, Cliente

# --- VISTA: LOGIN (PIN PAD) ---
@ensure_csrf_cookie
def pantalla_login(request):
    return render(request, 'pos/login.html')

def verificar_pin(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        pin = data.get('pin')
        
        try:
            # Buscar EMPLEADO con ese PIN
            # Usamos el nuevo modelo Empleado, no PerfilUsuario
            from .models import Empleado 
            empleado = Empleado.objects.get(pin=pin, activo=True)
            
            # Solo CAJERO o ADMIN pueden entrar al POS
            if empleado.rol in ['ADMIN', 'CAJERO']:
                if empleado.usuario:
                    # Asistencia automática de ENTRADA al iniciar sesión en caja.
                    hoy = timezone.localtime().date()
                    ya_abierta = Asistencia.objects.filter(
                        empleado=empleado,
                        fecha=hoy,
                        hora_salida__isnull=True
                    ).exists()
                    if not ya_abierta:
                        Asistencia.objects.create(empleado=empleado)

                    login(request, empleado.usuario)
                    return JsonResponse({
                        'status': 'ok',
                        'rol': empleado.rol,
                        'empleado_nombre': (empleado.nombre or '').strip(),
                    })
                else:
                    return JsonResponse({'status': 'error', 'mensaje': 'Empleado sin usuario de sistema asignado'})
            else:
                return JsonResponse({'status': 'error', 'mensaje': 'Rol no autorizado para POS'})

        except Empleado.DoesNotExist:
            return JsonResponse({'status': 'error', 'mensaje': 'PIN Incorrecto'})

    return JsonResponse({'status': 'error'}, status=400)

# --- VISTA: APERTURA DE CAJA ---
def apertura_caja(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')
        
    # Verificar si ya tiene caja abierta
    caja_abierta = CajaTurno.objects.filter(usuario=request.user, fecha_cierre__isnull=True).first()
    return render(request, 'pos/apertura.html', {'caja_abierta': caja_abierta})

def abrir_caja(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)

    if request.method == 'POST':
        # Si ya existe caja abierta, devolvemos datos para continuar sin crear otra.
        caja_abierta = CajaTurno.objects.filter(usuario=request.user, fecha_cierre__isnull=True).first()
        if caja_abierta:
            return JsonResponse({
                'status': 'ok',
                'ya_abierta': True,
                'base_inicial': f"{caja_abierta.base_inicial:.2f}"
            })

        data = json.loads(request.body)
        monto_raw = data.get('monto_inicial', 0)
        try:
            monto = Decimal(str(monto_raw)).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            return JsonResponse({'status': 'error', 'mensaje': 'Monto inicial inválido'}, status=400)
        if monto < 0:
            return JsonResponse({'status': 'error', 'mensaje': 'El monto inicial no puede ser negativo'}, status=400)
        
        caja = CajaTurno.objects.create(
            usuario=request.user,
            base_inicial=monto
        )
        return JsonResponse({
            'status': 'ok',
            'ya_abierta': False,
            'base_inicial': f"{caja.base_inicial:.2f}"
        })

# --- VISTA: CIERRE DE CAJA ---
def cierre_caja(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    caja = CajaTurno.objects.filter(usuario=request.user, fecha_cierre__isnull=True).first()
    if not caja:
        return redirect('pos_login')
    
    # Calcular totales reales del turno desde las ventas registradas
    from .models import Venta, MovimientoCaja
    ventas_turno = Venta.objects.filter(turno=caja)
    
    total_efectivo = ventas_turno.filter(metodo_pago='EFECTIVO').aggregate(total=Sum('total'))['total'] or 0
    total_transferencia = ventas_turno.filter(metodo_pago='TRANSFERENCIA').aggregate(total=Sum('total'))['total'] or 0
    total_tarjeta = ventas_turno.filter(metodo_pago='TARJETA').aggregate(total=Sum('total'))['total'] or 0
    
    # Movimientos de caja (ingresos/egresos)
    movimientos = MovimientoCaja.objects.filter(turno=caja)
    total_ingresos = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or 0
    total_egresos = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or 0
    
    # Guardar en el modelo para que el cierre tenga referencia
    caja.total_efectivo_sistema = total_efectivo
    caja.total_transferencia_sistema = total_transferencia
    caja.total_otros_sistema = total_tarjeta
    caja.save()
        
    return render(request, 'pos/cierre.html', {
        'caja': caja,
        'total_ingresos_caja': total_ingresos,
        'total_egresos_caja': total_egresos,
        'movimientos_caja': movimientos,
    })

def procesar_cierre(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)

    if request.method == 'POST':
        data = json.loads(request.body)
        conteo = data.get('conteo') # { "20": 5, "10": 10 ... }
        total_efectivo_real = data.get('total_declarado')
        
        caja = CajaTurno.objects.filter(usuario=request.user, fecha_cierre__isnull=True).first()
        if caja:
            # Asistencia automática de SALIDA al cerrar caja (si existe entrada abierta hoy).
            empleado = getattr(request.user, 'empleado', None)
            if empleado:
                hoy = timezone.localtime().date()
                asistencia_abierta = Asistencia.objects.filter(
                    empleado=empleado,
                    fecha=hoy,
                    hora_salida__isnull=True
                ).last()
                if asistencia_abierta:
                    asistencia_abierta.registrar_salida()

            caja.cerrar_caja(total_efectivo_real, conteo)
            caja_id = caja.id
            logout(request) # Cerrar sesión al terminar turno
            return JsonResponse({'status': 'ok', 'caja_id': caja_id})
            
    return JsonResponse({'status': 'error'})

# --- CERRAR SESIÓN MANUAL ---
def cerrar_sesion(request):
    logout(request)
    return redirect('pos_login')

# --- CLIENTES ---
def buscar_crear_cliente(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autenticado'}, status=401)

    def _cedula_valida(valor):
        return bool(valor) and valor.isdigit() and len(valor) in (10, 13)

    if request.method == 'POST':
        data = json.loads(request.body)
        cedula = data.get('cedula')
        if not _cedula_valida(cedula):
            return JsonResponse({'status': 'error', 'mensaje': 'C.I/RUC invalido (10 o 13 digitos)'}, status=400)
        
        cliente, created = Cliente.objects.get_or_create(
            cedula_ruc=cedula,
            defaults={
                'nombre': data.get('nombre'),
                'direccion': data.get('direccion', ''),
                'telefono': data.get('telefono', ''),
                'email': data.get('email', '')
            }
        )
        
        if not created:
            # Si ya existía, actualizamos datos si vinieron nuevos
            if data.get('nombre'): cliente.nombre = data.get('nombre')
            if data.get('direccion'): cliente.direccion = data.get('direccion')
            if data.get('telefono'): cliente.telefono = data.get('telefono')
            if data.get('email'): cliente.email = data.get('email')
            cliente.save()
            
        return JsonResponse({'status': 'ok', 'cliente_id': cliente.id, 'nombre': cliente.nombre})

    # Si es GET (búsqueda simple)
    cedula = request.GET.get('cedula')
    if cedula:
        if not _cedula_valida(cedula):
            return JsonResponse({'status': 'error', 'mensaje': 'C.I/RUC invalido (10 o 13 digitos)'}, status=400)
        try:
            c = Cliente.objects.get(cedula_ruc=cedula)
            return JsonResponse({
                'encontrado': True, 
                'id': c.id,
                'nombre': c.nombre, 
                'direccion': c.direccion, 
                'telefono': c.telefono, 
                'email': c.email
            })
        except Cliente.DoesNotExist:
            return JsonResponse({'encontrado': False})
    
    return JsonResponse({'status': 'error', 'mensaje': 'Parámetro cedula requerido'}, status=400)
