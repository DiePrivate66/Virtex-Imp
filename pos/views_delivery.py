import json
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from .models import Empleado, Venta
from .tasks import set_quote_and_notify


def delivery_portal(request):
    """
    Portal manual para cotizacion de delivery.
    Muestra pedidos en estado PENDIENTE_COTIZACION.
    """
    pedidos = Venta.objects.filter(estado='PENDIENTE_COTIZACION').order_by('-fecha')
    return render(request, 'pos/delivery_portal.html', {'pedidos': pedidos})


@csrf_exempt
def api_fijar_precio(request):
    """
    API para fijar precio manual de envio.
    Regla primer precio gana se aplica en task con lock.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Metodo no permitido'})

    try:
        data = json.loads(request.body)
        pedido_id = data.get('pedido_id')
        precio = data.get('precio', 0)

        venta = Venta.objects.get(id=pedido_id)
        empleado = request.user.empleado if request.user.is_authenticated and hasattr(request.user, 'empleado') else None
        if not empleado:
            empleado = Empleado.objects.filter(rol='DELIVERY', activo=True).first()
        if not empleado:
            return JsonResponse({'status': 'error', 'mensaje': 'No hay delivery configurado'})
        empleado_id = empleado.id

        try:
            Decimal(str(precio)).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            return JsonResponse({'status': 'error', 'mensaje': 'Precio invalido'})

        set_quote_and_notify.delay(venta.id, empleado_id, str(precio))
        return JsonResponse({'status': 'ok'})
    except Venta.DoesNotExist:
        return JsonResponse({'status': 'error', 'mensaje': 'Pedido no encontrado'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'mensaje': str(e)})


