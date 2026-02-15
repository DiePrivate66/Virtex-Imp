import json
from decimal import Decimal
from datetime import datetime
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from pos.models import Categoria, Producto, Venta, DetalleVenta, Cliente, CajaTurno


def esta_abierto():
    """Verifica si el local está dentro del horario de atención.
    L-S: 16:00-22:00 | Dom: 16:00-21:00"""
    ahora = timezone.localtime()
    dia = ahora.weekday()  # 0=Lunes, 6=Domingo
    hora = ahora.hour
    minuto = ahora.minute
    hora_decimal = hora + minuto / 60
    
    if dia == 6:  # Domingo
        return 16.0 <= hora_decimal < 21.0
    else:  # Lunes a Sábado
        return 16.0 <= hora_decimal < 22.0


def menu_cliente(request):
    """Vista principal de la PWA — renderiza el menú completo."""
    categorias = Categoria.objects.all()
    productos = Producto.objects.filter(activo=True).select_related('categoria')
    
    abierto = esta_abierto()
    ahora = timezone.localtime()
    dia = ahora.weekday()
    
    return render(request, 'pedidos/menu.html', {
        'categorias': categorias,
        'productos': productos,
        'local_abierto': abierto,
        'horario_hoy': '4:00 PM — 9:00 PM' if dia == 6 else '4:00 PM — 10:00 PM',
        'dia_hoy': ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'][dia],
    })


def api_productos(request):
    """API JSON: productos agrupados por categoría."""
    categorias = Categoria.objects.all()
    data = []
    for cat in categorias:
        productos = Producto.objects.filter(categoria=cat, activo=True)
        data.append({
            'id': cat.id,
            'nombre': cat.nombre,
            'productos': [
                {
                    'id': p.id,
                    'nombre': p.nombre,
                    'precio': str(p.precio),
                }
                for p in productos
            ]
        })
    return JsonResponse({'categorias': data})


@csrf_exempt
def api_crear_pedido(request):
    """API POST: crea un pedido desde la PWA del cliente."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Método no permitido'}, status=405)
    
    # Verificar horario de atención
    if not esta_abierto():
        ahora = timezone.localtime()
        dia = ahora.weekday()
        horario = '4:00 PM — 9:00 PM' if dia == 6 else '4:00 PM — 10:00 PM'
        return JsonResponse({
            'status': 'error',
            'mensaje': f'🕐 Lo sentimos, estamos cerrados. Nuestro horario es: {horario}'
        }, status=400)

    try:
        # Soportar FormData (con archivo) o JSON
        if request.content_type and 'multipart/form-data' in request.content_type:
            # FormData — viene del checkout con comprobante
            data = {
                'nombre': request.POST.get('nombre', 'CONSUMIDOR FINAL'),
                'cedula': request.POST.get('cedula', ''),
                'telefono': request.POST.get('telefono', ''),
                'direccion': request.POST.get('direccion', ''),
                'tipo_pedido': request.POST.get('tipo_pedido', 'DOMICILIO'),
                'metodo_pago': request.POST.get('metodo_pago', 'EFECTIVO'),
                'carrito': json.loads(request.POST.get('carrito', '[]')),
                'ubicacion_lat': request.POST.get('ubicacion_lat'),
                'ubicacion_lng': request.POST.get('ubicacion_lng'),
            }
            comprobante = request.FILES.get('comprobante')
        else:
            data = json.loads(request.body)
            comprobante = None

        carrito = data.get('carrito', [])
        if not carrito:
            return JsonResponse({'status': 'error', 'mensaje': 'El carrito está vacío'}, status=400)

        # Calcular total SERVER-SIDE (nunca confiar en el cliente)
        total = Decimal('0.00')
        items_validados = []
        for item in carrito:
            prod = Producto.objects.get(id=item['id'], activo=True)
            cantidad = int(item.get('cantidad', 1))
            subtotal = prod.precio * cantidad
            total += subtotal
            items_validados.append({
                'producto': prod,
                'cantidad': cantidad,
                'precio_unitario': prod.precio,
                'nombre_display': item.get('nombre', prod.nombre),
                'nota': item.get('nota', ''),
            })

        # Buscar o crear cliente si tiene cédula
        cliente = None
        cedula = data.get('cedula', '').strip()
        if cedula:
            cliente, _ = Cliente.objects.get_or_create(
                cedula_ruc=cedula,
                defaults={
                    'nombre': data.get('nombre', 'CONSUMIDOR FINAL'),
                    'telefono': data.get('telefono', ''),
                    'direccion': data.get('direccion', ''),
                }
            )

        # Buscar turno activo (para vincular al cierre de caja)
        turno_activo = CajaTurno.objects.filter(fecha_cierre__isnull=True).first()

        # Determinar estado inicial
        tipo_pedido = data.get('tipo_pedido', 'DOMICILIO')
        if tipo_pedido == 'DOMICILIO':
            estado_inicial = 'PENDIENTE_COTIZACION'  # Cajero debe cotizar envío
        else:
            estado_inicial = 'PENDIENTE'

        # Extraer coordenadas GPS si el cliente compartió ubicación
        lat = data.get('ubicacion_lat')
        lng = data.get('ubicacion_lng')

        # Crear la venta
        venta = Venta.objects.create(
            cliente=cliente,
            cliente_nombre=data.get('nombre', 'CONSUMIDOR FINAL'),
            telefono_cliente=data.get('telefono', ''),
            direccion_envio=data.get('direccion', ''),
            ubicacion_lat=float(lat) if lat else None,
            ubicacion_lng=float(lng) if lng else None,
            metodo_pago=data.get('metodo_pago', 'EFECTIVO'),
            tipo_pedido=tipo_pedido,
            total=total,
            monto_recibido=total if data.get('metodo_pago') == 'TRANSFERENCIA' else Decimal('0.00'),
            origen='WEB',
            estado=estado_inicial,
            turno=turno_activo,
            comprobante_foto=comprobante,
        )

        # Crear detalles
        for item_data in items_validados:
            prod = item_data['producto']
            nombre_display = item_data['nombre_display']
            nota_usuario = item_data['nota']

            # Extraer personalización del nombre (salsa)
            nota_final = ''
            if nombre_display != prod.nombre:
                nota_final = nombre_display.replace(prod.nombre, '').strip()
            if nota_usuario:
                nota_final = f"{nota_final} | {nota_usuario}" if nota_final else nota_usuario

            DetalleVenta.objects.create(
                venta=venta,
                producto=prod,
                cantidad=item_data['cantidad'],
                precio_unitario=item_data['precio_unitario'],
                nota=nota_final.strip(),
            )

        return JsonResponse({
            'status': 'ok',
            'pedido_id': venta.id,
            'mensaje': f'Pedido #{venta.id} recibido',
        })

    except Producto.DoesNotExist:
        return JsonResponse({'status': 'error', 'mensaje': 'Producto no encontrado o no disponible'}, status=400)
    except Exception as e:
        return JsonResponse({'status': 'error', 'mensaje': str(e)}, status=500)


def confirmacion_pedido(request, pedido_id):
    """Página de confirmación post-pedido."""
    venta = get_object_or_404(Venta, id=pedido_id, origen='WEB')
    return render(request, 'pedidos/confirmacion.html', {'venta': venta})
