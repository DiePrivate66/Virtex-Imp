import json
import threading
import re
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from .models import Categoria, Producto, Venta, DetalleVenta, Cliente, CajaTurno
from decimal import Decimal

def pos_index(request):
    # 1. Validar Login
    if not request.user.is_authenticated:
        return redirect('pos_login')

    # 2. Validar Caja Abierta
    caja_abierta = CajaTurno.objects.filter(usuario=request.user, fecha_cierre__isnull=True).first()
    if not caja_abierta:
        return redirect('pos_apertura')

    categorias = Categoria.objects.all()
    # Enviar productos con precio formateado si es necesario, aunque Django templating lo maneja
    productos = Producto.objects.filter(activo=True)
    
    # Obtener rol para ocultar botones
    rol = 'OTRO'
    if hasattr(request.user, 'empleado'):
        rol = request.user.empleado.rol
    
    return render(request, 'pos/index.html', {
        'categorias': categorias, 
        'productos': productos,
        'caja': caja_abierta, # Pasamos la caja para mostrar info si se requiere
        'rol': rol,
    })

def registrar_venta(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)

            def _cedula_valida(valor):
                return bool(valor) and valor.isdigit() and len(valor) in (10, 13)

            def _normalizar_referencia(valor):
                ref = (valor or '').upper().strip()
                ref = re.sub(r'\s+', '', ref)
                ref = re.sub(r'[^A-Z0-9\-_/]', '', ref)
                return ref[:40]

            def _normalizar_texto_simple(valor, max_len):
                txt = (valor or '').upper().strip()
                txt = re.sub(r'[^A-Z0-9 ]', '', txt)
                txt = re.sub(r'\s+', ' ', txt)
                return txt[:max_len]

            cedula_input = (data.get('cliente_cedula') or '').strip()
            consumidor_final = bool(data.get('consumidor_final'))
            metodo_pago = (data.get('metodo_pago') or '').upper().strip()
            total_venta = Decimal(str(data.get('total') or 0)).quantize(Decimal('0.01'))
            referencia_pago = _normalizar_referencia(data.get('referencia_pago'))
            tarjeta_tipo = _normalizar_texto_simple(data.get('tarjeta_tipo'), 12)
            tarjeta_marca = _normalizar_texto_simple(data.get('tarjeta_marca'), 20)
            
            # Buscar turno activo para asociar la venta
            turno_activo = CajaTurno.objects.filter(usuario=request.user, fecha_cierre__isnull=True).first()
            if not turno_activo:
                return JsonResponse({'status': 'error', 'mensaje': 'No hay caja activa para registrar ventas'}, status=400)

            if metodo_pago not in {'EFECTIVO', 'TRANSFERENCIA', 'TARJETA'}:
                return JsonResponse({'status': 'error', 'mensaje': 'Método de pago inválido'}, status=400)

            if total_venta <= 0:
                return JsonResponse({'status': 'error', 'mensaje': 'El total de la venta debe ser mayor a 0'}, status=400)

            if metodo_pago == 'TARJETA':
                if len(referencia_pago) < 6:
                    return JsonResponse({'status': 'error', 'mensaje': 'Referencia de tarjeta obligatoria (mínimo 6 caracteres)'}, status=400)
                if not tarjeta_tipo:
                    return JsonResponse({'status': 'error', 'mensaje': 'Tipo de tarjeta obligatorio (crédito o débito)'}, status=400)
                if tarjeta_tipo not in {'CREDITO', 'DEBITO'}:
                    return JsonResponse({'status': 'error', 'mensaje': 'Tipo de tarjeta inválido'}, status=400)

                hoy = timezone.localtime().date()
                existe_tarjeta = Venta.objects.filter(
                    origen='POS',
                    metodo_pago='TARJETA',
                    referencia_pago=referencia_pago,
                    total=total_venta,
                    fecha__date=hoy
                ).exclude(estado='CANCELADO').exclude(estado_pago='ANULADO').first()
                if existe_tarjeta:
                    return JsonResponse({
                        'status': 'error',
                        'mensaje': f'Pago con tarjeta duplicado detectado (venta #{existe_tarjeta.id})'
                    }, status=400)

            # --- Buscar Cliente si enviaron ID ---
            cliente = None
            if consumidor_final:
                cliente = None
            elif data.get('cliente_id'):
                cliente = Cliente.objects.filter(id=data.get('cliente_id')).first()
                if not cliente:
                    return JsonResponse({'status': 'error', 'mensaje': 'Cliente no encontrado'}, status=400)
                if not _cedula_valida(cliente.cedula_ruc):
                    return JsonResponse({'status': 'error', 'mensaje': 'C.I/RUC invalido (10 o 13 digitos)'}, status=400)
            else:
                if not _cedula_valida(cedula_input):
                    return JsonResponse({'status': 'error', 'mensaje': 'C.I/RUC invalido (10 o 13 digitos)'}, status=400)

            venta = Venta.objects.create(
                cliente_nombre=data.get('cliente_nombre', 'CONSUMIDOR FINAL'),
                cliente=cliente,
                metodo_pago=metodo_pago,
                referencia_pago=referencia_pago,
                tarjeta_tipo=tarjeta_tipo,
                tarjeta_marca=tarjeta_marca,
                estado_pago='APROBADO',
                total=total_venta,
                origen='POS',
                estado='COCINA',
                tipo_pedido=data.get('tipo_pedido', 'SERVIR'),
                monto_recibido=data.get('monto_recibido', 0),
                turno=turno_activo,
            )

            items = data.get('carrito', [])
            for item in items:
                prod = Producto.objects.get(id=item['id'])
                # Combinar nombre personalizado (con salsa) + notas del cajero
                nombre_display = item.get('nombre', prod.nombre)
                nota_usuario = item.get('nota', '')
                
                # Si el nombre del carrito incluye salsa (ej: "BONELESS (BBQ Original)")
                # lo extraemos y lo combinamos con las notas
                nota_final = ''
                if nombre_display != prod.nombre:
                    # Hay personalización (salsa u otra variación)
                    nota_final = nombre_display.replace(prod.nombre, '').strip()
                if nota_usuario:
                    nota_final = f"{nota_final} | {nota_usuario}" if nota_final else nota_usuario
                
                DetalleVenta.objects.create(
                    venta=venta,
                    producto=prod,
                    cantidad=item['cantidad'],
                    precio_unitario=item['precio'],
                    nota=nota_final.strip()
                )
            
            # Enviar factura electrónica por email si el cliente tiene correo
            if cliente and cliente.email:
                try:
                    html_email = render_to_string('pos/email/factura_email.html', {'venta': venta})
                    def enviar_email():
                        send_mail(
                            subject=f'RAMÓN - Comprobante de Venta #{venta.id}',
                            message=f'Adjunto su comprobante de venta #{venta.id} por ${venta.total}',
                            from_email=None,  # Usa DEFAULT_FROM_EMAIL de settings
                            recipient_list=[cliente.email],
                            html_message=html_email,
                            fail_silently=True,
                        )
                    # Enviar en hilo separado para no bloquear la respuesta
                    threading.Thread(target=enviar_email).start()
                except Exception:
                    pass  # No bloquear la venta si falla el email
            
            return JsonResponse({'status': 'ok', 'mensaje': f'Venta #{venta.id} Registrada', 'ticket_id': venta.id})
        except Exception as e:
            return JsonResponse({'status': 'error', 'mensaje': str(e)}, status=500)
    return JsonResponse({'status': 'error', 'mensaje': 'Método no permitido'}, status=405)


# --- PANEL DE PEDIDOS WEB ---
def _timed_out_quote_count():
    return Venta.objects.filter(
        origen='WEB',
        estado='PENDIENTE_COTIZACION',
        delivery_quote_deadline_at__isnull=False,
        delivery_quote_deadline_at__lt=timezone.now(),
    ).count()


def panel_pedidos_web(request):
    if not request.user.is_authenticated:
        return redirect('pos_login')

    pedidos = Venta.objects.filter(origen='WEB').exclude(estado='CANCELADO').order_by('-fecha')[:50]
    return render(
        request,
        'pos/pedidos_web.html',
        {
            'pedidos': pedidos,
            'timed_out_quote_count': _timed_out_quote_count(),
        },
    )


def api_actualizar_pedido(request):
    """API para que el cajero cambie estado y/o costo de envío de un pedido WEB."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'mensaje': 'Método no permitido'}, status=405)
    
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=401)
    
    try:
        data = json.loads(request.body)
        pedido_id = data.get('pedido_id')
        venta = Venta.objects.get(id=pedido_id)
        
        # Actualizar estado si viene
        nuevo_estado = data.get('estado')
        if nuevo_estado:
            venta.estado = nuevo_estado
        
        # Actualizar costo de envío si viene
        costo = data.get('costo_envio')
        if costo is not None:
            venta.costo_envio = Decimal(str(costo))
        
        venta.save()
        
        return JsonResponse({
            'status': 'ok',
            'estado': venta.estado,
            'estado_display': venta.get_estado_display(),
        })
    except Venta.DoesNotExist:
        return JsonResponse({'status': 'error', 'mensaje': 'Pedido no encontrado'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'mensaje': str(e)}, status=500)


def api_pedidos_web_json(request):
    """API JSON para polling de pedidos WEB (auto-refresh)."""
    pedidos = Venta.objects.filter(origen='WEB').exclude(
        estado__in=['CANCELADO', 'LISTO']
    ).order_by('-fecha')[:50]

    timed_out_quote_count = _timed_out_quote_count()
    data = []
    for p in pedidos:
        items = []
        for d in p.detalles.all():
            items.append({
                'nombre': d.producto.nombre,
                'cantidad': d.cantidad,
                'nota': d.nota,
                'subtotal': str(d.subtotal),
            })
        data.append({
            'id': p.id,
            'estado': p.estado,
            'estado_display': p.get_estado_display(),
            'estado_pago': p.estado_pago,
            'cliente_nombre': p.cliente_nombre,
            'telefono': p.telefono_cliente,
            'direccion': p.direccion_envio,
            'tipo_pedido': p.tipo_pedido,
            'tipo_pedido_display': p.get_tipo_pedido_display(),
            'metodo_pago': p.metodo_pago,
            'metodo_pago_display': p.get_metodo_pago_display(),
            'referencia_pago': p.referencia_pago,
            'tarjeta_tipo': p.tarjeta_tipo,
            'tarjeta_marca': p.tarjeta_marca,
            'total': str(p.total),
            'costo_envio': str(p.costo_envio),
            'comprobante': p.comprobante_foto.url if p.comprobante_foto else None,
            'fecha': p.fecha.strftime('%H:%M'),
            'items': items,
        })
    
    return JsonResponse({'pedidos': data, 'count': len(data), 'timed_out_quote_count': timed_out_quote_count})

# --- VISTAS DE IMPRESIÓN ---
def imprimir_ticket(request, venta_id):
    venta = Venta.objects.get(id=venta_id)
    # IVA 15% desglosado — los precios de RAMÓN ya incluyen IVA
    subtotal_sin_iva = (venta.total / Decimal('1.15')).quantize(Decimal('0.01'))
    iva_valor = (venta.total - subtotal_sin_iva).quantize(Decimal('0.01'))
    return render(request, 'pos/print/ticket_consumidor.html', {
        'venta': venta,
        'subtotal_sin_iva': subtotal_sin_iva,
        'iva_valor': iva_valor,
    })

def imprimir_comanda(request, venta_id):
    venta = Venta.objects.get(id=venta_id)
    return render(request, 'pos/print/comanda_cocina.html', {'venta': venta})

def imprimir_venta_completa(request, venta_id):
    venta = Venta.objects.get(id=venta_id)
    return render(request, 'pos/print/venta_completa.html', {'venta': venta})

def imprimir_cierre(request, caja_id):
    from django.db.models import Sum, Count
    from django.utils import timezone
    from .models import MovimientoCaja
    
    caja = get_object_or_404(CajaTurno, id=caja_id)
    ventas = Venta.objects.filter(turno=caja)
    
    # Totales por método
    total_efectivo = ventas.filter(metodo_pago='EFECTIVO').aggregate(t=Sum('total'))['t'] or 0
    total_transferencia = ventas.filter(metodo_pago='TRANSFERENCIA').aggregate(t=Sum('total'))['t'] or 0
    total_tarjeta = ventas.filter(metodo_pago='TARJETA').aggregate(t=Sum('total'))['t'] or 0
    
    # Conteos
    num_efectivo = ventas.filter(metodo_pago='EFECTIVO').count()
    num_transferencia = ventas.filter(metodo_pago='TRANSFERENCIA').count()
    num_tarjeta = ventas.filter(metodo_pago='TARJETA').count()
    num_ventas = ventas.count()
    
    total_ventas = total_efectivo + total_transferencia + total_tarjeta

    # Reconciliación de tarjeta por referencia/lote
    tarjetas_por_referencia = list(
        ventas.filter(metodo_pago='TARJETA')
        .exclude(referencia_pago='')
        .values('referencia_pago', 'tarjeta_tipo', 'tarjeta_marca')
        .annotate(cantidad=Count('id'), total=Sum('total'))
        .order_by('-cantidad', 'referencia_pago')
    )
    
    # Movimientos de caja (ingresos/egresos)
    movimientos = MovimientoCaja.objects.filter(turno=caja)
    total_ingresos_caja = movimientos.filter(tipo='INGRESO').aggregate(t=Sum('monto'))['t'] or 0
    total_egresos_caja = movimientos.filter(tipo='EGRESO').aggregate(t=Sum('monto'))['t'] or 0
    
    esperado = caja.base_inicial + total_efectivo + total_ingresos_caja - total_egresos_caja
    
    # Preparar conteo detallado de denominaciones
    conteo_detalle = []
    if caja.conteo_billetes:
        for denom, cantidad in sorted(caja.conteo_billetes.items(), key=lambda x: float(x[0]), reverse=True):
            subtotal = float(denom) * int(cantidad)
            conteo_detalle.append((denom, cantidad, subtotal))
    
    # Nombre del cajero
    cajero_nombre = caja.usuario.get_full_name() or caja.usuario.username
    
    context = {
        'caja': caja,
        'cajero_nombre': cajero_nombre,
        'total_efectivo': total_efectivo,
        'total_transferencia': total_transferencia,
        'total_tarjeta': total_tarjeta,
        'num_efectivo': num_efectivo,
        'num_transferencia': num_transferencia,
        'num_tarjeta': num_tarjeta,
        'num_ventas': num_ventas,
        'total_ventas': total_ventas,
        'esperado': esperado,
        'total_ingresos_caja': total_ingresos_caja,
        'total_egresos_caja': total_egresos_caja,
        'conteo_detalle': conteo_detalle,
        'tarjetas_por_referencia': tarjetas_por_referencia,
        'ahora': timezone.now(),
    }
    
    return render(request, 'pos/print/reporte_cierre.html', context)

def imprimir_etiqueta_delivery(request, venta_id):
    venta = Venta.objects.get(id=venta_id)
    return render(request, 'pos/print/etiqueta_delivery.html', {'venta': venta})
