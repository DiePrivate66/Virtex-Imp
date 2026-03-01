import { CommonModule } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectorRef, Component, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { CarritoItem, CategoriaConProductos, CrearPedidoPayload, Producto } from '../../models/pedido.models';
import { CartService } from '../../services/cart';
import { PedidoApiService } from '../../services/pedido-api';

type TipoPedido = 'DOMICILIO' | 'LLEVAR';
type MetodoPago = 'EFECTIVO' | 'TRANSFERENCIA';

@Component({
  selector: 'app-customer-menu',
  imports: [CommonModule, FormsModule],
  templateUrl: './customer-menu.html',
  styleUrl: './customer-menu.css'
})
export class CustomerMenuComponent implements OnInit {
  categorias: CategoriaConProductos[] = [];
  categoriaSeleccionada = 'todos';

  loadingCatalog = true;
  catalogError = '';
  checkoutOpen = false;
  sending = false;
  formError = '';

  tipoPedido: TipoPedido = 'DOMICILIO';
  metodoPago: MetodoPago = 'EFECTIVO';
  comprobanteFile: File | null = null;

  nombre = '';
  telefono = '';
  cedula = '';
  direccion = '';

  gpsLat: number | null = null;
  gpsLng: number | null = null;
  gpsEstado: 'none' | 'loading' | 'ok' | 'error' = 'none';
  gpsError = '';

  constructor(
    public readonly cart: CartService,
    private readonly api: PedidoApiService,
    private readonly router: Router,
    private readonly cdr: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    this.loadCatalog();
  }

  get productosFiltrados(): Producto[] {
    const todos = this.categorias.flatMap((cat) => cat.productos);
    if (this.categoriaSeleccionada === 'todos') return todos;
    const cat = this.categorias.find((c) => String(c.id) === this.categoriaSeleccionada);
    return cat?.productos || [];
  }

  trackProducto(_: number, producto: Producto): number {
    return producto.id;
  }

  trackCarrito(index: number): number {
    return index;
  }

  selectCategory(value: string): void {
    this.categoriaSeleccionada = value;
  }

  addProducto(producto: Producto): void {
    this.cart.addProduct(producto);
  }

  aumentar(index: number): void {
    this.cart.increase(index);
  }

  disminuir(index: number): void {
    this.cart.decrease(index);
  }

  updateNote(index: number, value: string): void {
    this.cart.updateNote(index, value);
  }

  abrirCheckout(): void {
    this.checkoutOpen = true;
    this.formError = '';
  }

  cerrarCheckout(): void {
    this.checkoutOpen = false;
    this.formError = '';
  }

  onComprobanteChange(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0] || null;
    this.comprobanteFile = file;
  }

  obtenerGps(): void {
    if (!('geolocation' in navigator)) {
      this.gpsEstado = 'error';
      this.gpsError = 'Tu dispositivo no soporta geolocalizacion.';
      this.cdr.detectChanges();
      return;
    }

    this.gpsEstado = 'loading';
    this.gpsError = '';
    this.cdr.detectChanges();

    navigator.geolocation.getCurrentPosition(
      (position) => {
        this.gpsLat = position.coords.latitude;
        this.gpsLng = position.coords.longitude;
        this.gpsEstado = 'ok';
        this.cdr.detectChanges();
      },
      (error) => {
        this.gpsEstado = 'error';
        this.gpsError = error.message || 'No se pudo obtener la ubicacion';
        this.cdr.detectChanges();
      },
      { enableHighAccuracy: true, timeout: 10000 }
    );
  }

  formatMoney(value: number): string {
    return `$${value.toFixed(2)}`;
  }

  enviarPedido(): void {
    this.formError = '';

    const items = this.cart.items();
    if (!items.length) {
      this.formError = 'Tu carrito esta vacio.';
      return;
    }

    if (!this.nombre.trim()) {
      this.formError = 'El nombre es obligatorio.';
      return;
    }

    if (!this.telefono.trim()) {
      this.formError = 'El telefono es obligatorio.';
      return;
    }

    if (this.tipoPedido === 'DOMICILIO' && !this.direccion.trim()) {
      this.formError = 'La direccion es obligatoria para delivery.';
      return;
    }

    if (this.metodoPago === 'TRANSFERENCIA' && !this.comprobanteFile) {
      this.formError = 'Debes adjuntar el comprobante de transferencia.';
      return;
    }

    const payload: CrearPedidoPayload = {
      nombre: this.nombre.trim().toUpperCase(),
      telefono: this.telefono.trim(),
      cedula: this.cedula.trim(),
      direccion: this.tipoPedido === 'DOMICILIO' ? this.direccion.trim() : '',
      tipo_pedido: this.tipoPedido,
      metodo_pago: this.metodoPago,
      carrito: items,
      ubicacion_lat: this.gpsLat,
      ubicacion_lng: this.gpsLng
    };

    this.sending = true;
    this.cdr.detectChanges();

    this.api.crearPedido(payload, this.comprobanteFile).subscribe({
      next: (resp) => {
        this.sending = false;
        if (resp.status !== 'ok' || !resp.pedido_id) {
          this.formError = resp.mensaje || 'No se pudo registrar el pedido.';
          this.cdr.detectChanges();
          return;
        }

        this.cart.clear();
        this.checkoutOpen = false;
        this.comprobanteFile = null;
        this.cdr.detectChanges();
        this.router.navigate(['/confirmacion', resp.pedido_id]);
      },
      error: (error: HttpErrorResponse) => {
        this.sending = false;
        this.formError =
          error?.error?.mensaje ||
          error?.message ||
          'No se pudo enviar el pedido. Intenta nuevamente.';
        this.cdr.detectChanges();
      }
    });
  }

  private loadCatalog(): void {
    this.loadingCatalog = true;
    this.catalogError = '';
    this.cdr.detectChanges();

    this.api.getCategorias().subscribe({
      next: (cats) => {
        this.categorias = cats;
        this.loadingCatalog = false;
        this.cdr.detectChanges();
      },
      error: (error: HttpErrorResponse) => {
        this.catalogError =
          error?.error?.mensaje ||
          'No se pudo cargar el menu. Verifica que Django este encendido.';
        this.loadingCatalog = false;
        this.cdr.detectChanges();
      }
    });
  }
}
