from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse

from django.contrib.auth.models import User
from django.utils import timezone
import re
import json
from .models import Empleado, Asistencia

# --- VISTAS DE GESTIÓN (CRUD EMPLEADOS) ---
def lista_empleados(request):
    # Validar que sea admin o tenga permiso (Simple check por ahora)
    if not request.user.is_authenticated: # Idealmente checkear rol ADMIN
        return redirect('pos_login')
        
    empleados = Empleado.objects.all().order_by('-activo', 'nombre')
    return render(request, 'pos/empleados/lista.html', {'empleados': empleados})

def guardar_empleado(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=401)

    if request.method == 'POST':
        data = json.loads(request.body)
        empleado_id = data.get('id')
        cedula_raw = str(data.get('cedula') or '').strip()
        cedula_limpia = re.sub(r'\D', '', cedula_raw)
        cedula_valor = cedula_limpia if cedula_limpia else None

        if cedula_valor and not re.fullmatch(r'\d{10}', cedula_valor):
            return JsonResponse({'status': 'error', 'mensaje': 'La cédula debe tener exactamente 10 dígitos'})
        
        try:
            if empleado_id: # Edición
                emp = Empleado.objects.get(id=empleado_id)
                
                # Validar PIN único (excluyendo el mismo empleado)
                new_pin = data.get('pin')
                if new_pin != emp.pin and Empleado.objects.filter(pin=new_pin).exclude(id=emp.id).exists():
                    return JsonResponse({'status': 'error', 'mensaje': 'El PIN ya está en uso por otro empleado'})
                
                emp.nombre = data.get('nombre')
                emp.cedula = cedula_valor
                emp.telefono = data.get('telefono')
                emp.direccion = data.get('direccion')
                emp.rol = data.get('rol')
                emp.pin = new_pin
                emp.activo = data.get('activo', True)
            else: # Creación
                # Validar PIN único
                if Empleado.objects.filter(pin=data.get('pin')).exists():
                    return JsonResponse({'status': 'error', 'mensaje': 'El PIN ya está en uso'})
                
                emp = Empleado(
                    nombre=data.get('nombre'),
                    cedula=cedula_valor,
                    telefono=data.get('telefono'),
                    direccion=data.get('direccion'),
                    rol=data.get('rol'),
                    pin=data.get('pin')
                )
            
            emp.save()
            
            # --- GESTIÓN AUTOMÁTICA DE USUARIO DJANGO ---
            if emp.rol in ['ADMIN', 'CAJERO']:
                if not emp.usuario:
                    username = f"emp_{emp.pin}"
                    user, created = User.objects.get_or_create(username=username)
                    if created:
                        user.set_password(emp.pin)
                        user.first_name = emp.nombre.split()[0] if emp.nombre else ''
                        user.save()
                    emp.usuario = user
                    emp.save()
                else:
                    # Actualizar username si cambió el PIN
                    user = emp.usuario
                    user.username = f"emp_{emp.pin}"
                    user.set_password(emp.pin)
                    user.save()
            else:
                # Si cambiaron a un rol que no necesita usuario, desvinculamos
                if emp.usuario:
                    emp.usuario = None
                    emp.save()
            
            return JsonResponse({'status': 'ok'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'mensaje': str(e)})

    return JsonResponse({'status': 'error'}, status=400)

# --- API ASISTENCIA (PUBLIC/PIN PAD) ---
def registrar_asistencia(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        pin = data.get('pin')
        accion = data.get('accion') # 'ENTRADA' o 'SALIDA'
        
        try:
            empleado = Empleado.objects.get(pin=pin, activo=True)
            
            if accion == 'ENTRADA':
                # Validar si ya tiene entrada hoy sin salida
                hoy = timezone.localtime().date()
                existe = Asistencia.objects.filter(empleado=empleado, fecha=hoy, hora_salida__isnull=True).exists()
                if existe:
                    return JsonResponse({'status': 'error', 'mensaje': f'Hola {empleado.nombre}, ya marcaste entrada hoy.'})
                
                Asistencia.objects.create(empleado=empleado)
                return JsonResponse({'status': 'ok', 'mensaje': f'Bienvenido/a {empleado.nombre}. Entrada registrada.'})
                
            elif accion == 'SALIDA':
                hoy = timezone.localtime().date()
                asistencia = Asistencia.objects.filter(empleado=empleado, fecha=hoy, hora_salida__isnull=True).last()
                if not asistencia:
                    return JsonResponse({'status': 'error', 'mensaje': f'Error: No tienes una entrada registrada hoy o ya marcaste salida.'})
                
                asistencia.registrar_salida()
                return JsonResponse({'status': 'ok', 'mensaje': f'Hasta luego {empleado.nombre}. Salida registrada.'})
                
        except Empleado.DoesNotExist:
            return JsonResponse({'status': 'error', 'mensaje': 'PIN no encontrado'})
            
    return JsonResponse({'status': 'error'}, status=400)
