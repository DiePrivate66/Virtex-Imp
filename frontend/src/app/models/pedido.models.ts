export interface Producto {
  id: number;
  nombre: string;
  precio: number;
}

export interface CategoriaConProductos {
  id: number;
  nombre: string;
  productos: Producto[];
}

export interface CarritoItem {
  id: number;
  nombre: string;
  precio: number;
  cantidad: number;
  nota: string;
}

export interface CrearPedidoPayload {
  nombre: string;
  telefono: string;
  cedula: string;
  email?: string;
  direccion: string;
  tipo_pedido: 'DOMICILIO' | 'LLEVAR';
  metodo_pago: 'EFECTIVO' | 'TRANSFERENCIA';
  carrito: CarritoItem[];
  ubicacion_lat?: number | null;
  ubicacion_lng?: number | null;
}

export interface CrearPedidoResponse {
  status: 'ok' | 'error';
  pedido_id?: number;
  mensaje: string;
}

export interface EstadoPedidoResponse {
  pedido_id: number;
  estado: string;
  estado_display: string;
  tipo_pedido: 'DOMICILIO' | 'LLEVAR' | 'SERVIR';
  metodo_pago: string;
  cliente_nombre: string;
  telefono_cliente: string;
  total: string;
  costo_envio: string;
  total_con_envio: string;
  tiempo_estimado_minutos: number | null;
  minutos_restantes_estimados: number | null;
  cliente_reporto_recibido: boolean;
  repartidor_confirmo_entrega: boolean;
  puede_reportar_recibido: boolean;
  esperando_confirmacion_delivery: boolean;
}
