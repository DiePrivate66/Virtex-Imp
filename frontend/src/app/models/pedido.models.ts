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