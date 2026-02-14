import json
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from .models import Categoria, Producto, Venta, DetalleVenta, CajaTurno

def pos_index(request):
    categorias = Categoria.objects.all()
    productos = Producto.objects.filter(activo=True)
    return render(request, 'pos/index.html', {
        'categorias': categorias, 
        'productos': productos
    })

@csrf_exempt # Desactivamos CSRF solo por facilidad en este sprint (luego lo aseguramos)
def registrar_venta(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            
            # 1. Crear la Venta
            venta = Venta.objects.create(
                cliente_nombre=data.get('cliente_nombre', 'CONSUMIDOR FINAL'),
                metodo_pago=data.get('metodo_pago'),
                total=data.get('total'),
                origen='POS',
                estado='COCINA' # Pasa directo a cocina
            )
            
            # 2. Guardar cada producto (Detalle)
            items = data.get('carrito', [])
            for item in items:
                prod = Producto.objects.get(id=item['id'])
                DetalleVenta.objects.create(
                    venta=venta,
                    producto=prod,
                    cantidad=item['cantidad'],
                    precio_unitario=item['precio']
                )
            
            return JsonResponse({'status': 'ok', 'mensaje': f'Venta #{venta.id} Registrada', 'ticket_id': venta.id})
            
        except Exception as e:
            return JsonResponse({'status': 'error', 'mensaje': str(e)}, status=500)
    
    return JsonResponse({'status': 'error', 'mensaje': 'Método no permitido'}, status=405)