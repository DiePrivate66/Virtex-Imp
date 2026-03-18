import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from pos.models import CajaTurno, Categoria, Cliente, DetalleVenta, Producto, Venta, WhatsAppConversation
from pos.tasks import process_delivery_quote_timeout, send_delivery_quote_requests
from pos.whatsapp_utils import normalize_phone_to_e164

logger = logging.getLogger(__name__)


def esta_abierto():
    """Verifica si el local esta dentro del horario de atencion.
    L-S: 16:00-22:00 | Dom: 16:00-21:00"""
    if not getattr(settings, 'ENABLE_BUSINESS_HOURS', True):
        return True

    ahora = timezone.localtime()
    dia = ahora.weekday()  # 0=Lunes, 6=Domingo
    hora_decimal = ahora.hour + (ahora.minute / 60)

    if dia == 6:  # Domingo
        return 16.0 <= hora_decimal < 21.0
    return 16.0 <= hora_decimal < 22.0


def menu_cliente(request):
    """Vista principal de la PWA - renderiza el menu completo."""
    categorias = Categoria.objects.all()
    productos = Producto.objects.filter(activo=True).select_related('categoria')

    abierto = esta_abierto()
    ahora = timezone.localtime()
    dia = ahora.weekday()

    return render(
        request,
        'pedidos/menu.html',
        {
            'categorias': categorias,
            'productos': productos,
            'local_abierto': abierto,
            'horario_hoy': '4:00 PM - 9:00 PM' if dia == 6 else '4:00 PM - 10:00 PM',
            'dia_hoy': ['Lunes', 'Martes', 'Miercoles', 'Jueves', 'Viernes', 'Sabado', 'Domingo'][dia],
        },
    )


def api_productos(request):
    """API JSON: productos agrupados por categoria."""
    categorias = Categoria.objects.all()
    data = []
    for cat in categorias:
        productos = Producto.objects.filter(categoria=cat, activo=True)
        data.append(
            {
                'id': cat.id,
                'nombre': cat.nombre,
                'productos': [
                    {
                        'id': p.id,
                        'nombre': p.nombre,
                        'precio': str(p.precio),
                    }
                    for p in productos
                ],
            }
        )
    return JsonResponse({'categorias': data})


@csrf_exempt
def api_crear_pedido(request):
    """API POST: crea un pedido desde la PWA del cliente."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Metodo no permitido'}, status=405)

    if not esta_abierto():
        ahora = timezone.localtime()
        dia = ahora.weekday()
        horario = '4:00 PM - 9:00 PM' if dia == 6 else '4:00 PM - 10:00 PM'
        return JsonResponse(
            {
                'status': 'error',
                'mensaje': f'Lo sentimos, estamos cerrados. Nuestro horario es: {horario}',
            },
            status=400,
        )

    try:
        if request.content_type and 'multipart/form-data' in request.content_type:
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
            return JsonResponse({'status': 'error', 'mensaje': 'El carrito esta vacio'}, status=400)

        total = Decimal('0.00')
        items_validados = []
        for item in carrito:
            prod = Producto.objects.get(id=item['id'], activo=True)
            cantidad = int(item.get('cantidad', 1))
            subtotal = prod.precio * cantidad
            total += subtotal
            items_validados.append(
                {
                    'producto': prod,
                    'cantidad': cantidad,
                    'precio_unitario': prod.precio,
                    'nombre_display': item.get('nombre', prod.nombre),
                    'nota': item.get('nota', ''),
                }
            )

        cliente = None
        cedula = data.get('cedula', '').strip()
        if cedula:
            cliente, _ = Cliente.objects.get_or_create(
                cedula_ruc=cedula,
                defaults={
                    'nombre': data.get('nombre', 'CONSUMIDOR FINAL'),
                    'telefono': data.get('telefono', ''),
                    'direccion': data.get('direccion', ''),
                },
            )

        turno_activo = CajaTurno.objects.filter(fecha_cierre__isnull=True).first()

        tipo_pedido = data.get('tipo_pedido', 'DOMICILIO')
        estado_inicial = 'PENDIENTE_COTIZACION' if tipo_pedido == 'DOMICILIO' else 'PENDIENTE'

        lat = data.get('ubicacion_lat')
        lng = data.get('ubicacion_lng')
        telefono_raw = data.get('telefono', '')

        venta = Venta.objects.create(
            cliente=cliente,
            cliente_nombre=data.get('nombre', 'CONSUMIDOR FINAL'),
            telefono_cliente=telefono_raw,
            telefono_cliente_e164=normalize_phone_to_e164(telefono_raw),
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
            confirmacion_cliente='PENDIENTE',
            delivery_quote_deadline_at=(
                timezone.now() + timedelta(seconds=settings.DELIVERY_QUOTE_TIMEOUT_SECONDS)
                if tipo_pedido == 'DOMICILIO'
                else None
            ),
        )

        for item_data in items_validados:
            prod = item_data['producto']
            nombre_display = item_data['nombre_display']
            nota_usuario = item_data['nota']

            nota_final = ''
            if nombre_display != prod.nombre:
                nota_final = nombre_display.replace(prod.nombre, '').strip()
            if nota_usuario:
                nota_final = f'{nota_final} | {nota_usuario}' if nota_final else nota_usuario

            DetalleVenta.objects.create(
                venta=venta,
                producto=prod,
                cantidad=item_data['cantidad'],
                precio_unitario=item_data['precio_unitario'],
                nota=nota_final.strip(),
            )

        if venta.telefono_cliente_e164:
            conv, _ = WhatsAppConversation.objects.get_or_create(telefono_e164=venta.telefono_cliente_e164)
            conv.venta = venta
            conv.save(update_fields=['venta'])

        if tipo_pedido == 'DOMICILIO':
            send_delivery_quote_requests.delay(venta.id)
            process_delivery_quote_timeout.apply_async(
                args=[venta.id], countdown=settings.DELIVERY_QUOTE_TIMEOUT_SECONDS
            )

        return JsonResponse(
            {
                'status': 'ok',
                'pedido_id': venta.id,
                'mensaje': f'Pedido #{venta.id} recibido',
            }
        )

    except Producto.DoesNotExist:
        return JsonResponse({'status': 'error', 'mensaje': 'Producto no encontrado o no disponible'}, status=400)
    except Exception:
        logger.exception('Error inesperado creando pedido web')
        return JsonResponse(
            {'status': 'error', 'mensaje': 'No se pudo crear el pedido. Intenta nuevamente.'},
            status=500,
        )


def confirmacion_pedido(request, pedido_id):
    """Pagina de confirmacion post-pedido."""
    venta = get_object_or_404(Venta, id=pedido_id, origen='WEB')
    return render(request, 'pedidos/confirmacion.html', {'venta': venta})


