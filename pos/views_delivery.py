import json
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from .models import Venta

def delivery_portal(request):
    """
    Portal para el Jefe de Delivery.
    Muestra pedidos en estado 'PENDIENTE_COTIZACION'.
    """
    # No requiere login de Django para facilitar acceso rápido desde WhatsApp
    # Se podría proteger con un token en URL si fuera necesario
    pedidos = Venta.objects.filter(estado='PENDIENTE_COTIZACION').order_by('-fecha')
    
    return render(request, 'pos/delivery_portal.html', {'pedidos': pedidos})

@csrf_exempt
def api_fijar_precio(request):
    """
    API para que el delivery fije el precio.
    Cambia estado a 'PENDIENTE' para que el cliente confirme/vea.
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            pedido_id = data.get('pedido_id')
            precio = float(data.get('precio', 0))
            
            venta = Venta.objects.get(id=pedido_id)
            venta.costo_envio = precio
            venta.estado = 'PENDIENTE' # Pasa a pendiente para que Cajero/Cliente confirmen
            venta.save()
            
            return JsonResponse({'status': 'ok'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'mensaje': str(e)})
    return JsonResponse({'status': 'error', 'mensaje': 'Método no permitido'})
