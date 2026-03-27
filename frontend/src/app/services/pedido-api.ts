import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { map, Observable } from 'rxjs';
import {
  CategoriaConProductos,
  CrearPedidoPayload,
  CrearPedidoResponse,
  EstadoPedidoResponse
} from '../models/pedido.models';
import { environment } from '../../environments/environment';

@Injectable({ providedIn: 'root' })
export class PedidoApiService {
  private readonly apiBase = environment.apiBaseUrl;

  constructor(private readonly http: HttpClient) {}

  getCategorias(): Observable<CategoriaConProductos[]> {
    return this.http
      .get<{ categorias: CategoriaConProductos[] }>(`${this.apiBase}/productos/`)
      .pipe(
        map((resp) =>
          (resp.categorias || []).map((cat) => ({
            ...cat,
            productos: (cat.productos || []).map((prod) => ({
              ...prod,
              precio: Number(prod.precio)
            }))
          }))
        )
      );
  }

  crearPedido(payload: CrearPedidoPayload, comprobante?: File | null): Observable<CrearPedidoResponse> {
    if (payload.metodo_pago === 'TRANSFERENCIA' && comprobante) {
      const formData = new FormData();
      formData.append('nombre', payload.nombre);
      formData.append('cedula', payload.cedula || '');
      formData.append('telefono', payload.telefono);
      formData.append('email', payload.email || '');
      formData.append('direccion', payload.direccion || '');
      formData.append('tipo_pedido', payload.tipo_pedido);
      formData.append('metodo_pago', payload.metodo_pago);
      formData.append('carrito', JSON.stringify(payload.carrito));
      if (payload.ubicacion_lat != null) {
        formData.append('ubicacion_lat', String(payload.ubicacion_lat));
      }
      if (payload.ubicacion_lng != null) {
        formData.append('ubicacion_lng', String(payload.ubicacion_lng));
      }
      formData.append('comprobante', comprobante);
      return this.http.post<CrearPedidoResponse>(`${this.apiBase}/crear/`, formData);
    }

    return this.http.post<CrearPedidoResponse>(`${this.apiBase}/crear/`, payload, {
      headers: { 'Content-Type': 'application/json' }
    });
  }

  getEstadoPedido(pedidoId: string | number): Observable<EstadoPedidoResponse> {
    return this.http.get<EstadoPedidoResponse>(`${this.apiBase}/pedidos/${pedidoId}/estado/`);
  }

  reportarPedidoRecibido(pedidoId: string | number): Observable<{ status: 'ok' | 'error'; mensaje: string }> {
    return this.http.post<{ status: 'ok' | 'error'; mensaje: string }>(
      `${this.apiBase}/pedidos/${pedidoId}/recibido/`,
      {}
    );
  }
}
