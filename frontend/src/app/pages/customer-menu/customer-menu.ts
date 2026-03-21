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
  private readonly preferredGpsAccuracyMeters = 120;
  private readonly maxGpsAccuracyMeters = 350;
  private readonly fabSwipeThreshold = 70;
  private resetCancelFabTimer: number | null = null;
  private fabTouchStartX: number | null = null;
  private fabTouchStartY: number | null = null;

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

  gpsLat: number | null = null;
  gpsLng: number | null = null;
  gpsAccuracy: number | null = null;
  gpsEstado: 'none' | 'loading' | 'ok' | 'error' = 'none';
  gpsError = '';
  gpsWarning = '';
  cancelFabMode = false;

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
    this.cancelFabMode = false;
    this.clearCancelFabTimer();
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
    if (this.cancelFabMode) {
      this.cart.clear();
      this.cancelFabMode = false;
      this.clearCancelFabTimer();
      return;
    }

    this.checkoutOpen = true;
    this.formError = '';
    if (this.tipoPedido === 'DOMICILIO' && (this.gpsLat == null || this.gpsLng == null)) {
      this.obtenerGps();
    }
  }

  cerrarCheckout(): void {
    this.checkoutOpen = false;
    this.formError = '';
  }

  onFabTouchStart(event: TouchEvent): void {
    const touch = event.touches[0];
    if (!touch) return;
    this.fabTouchStartX = touch.clientX;
    this.fabTouchStartY = touch.clientY;
  }

  onFabTouchEnd(event: TouchEvent): void {
    const touch = event.changedTouches[0];
    if (!touch || this.fabTouchStartX == null || this.fabTouchStartY == null) {
      this.resetFabTouch();
      return;
    }

    const deltaX = touch.clientX - this.fabTouchStartX;
    const deltaY = Math.abs(touch.clientY - this.fabTouchStartY);
    this.resetFabTouch();

    if (deltaY > 50) return;
    if (Math.abs(deltaX) < this.fabSwipeThreshold) return;

    this.cancelFabMode = deltaX < 0;
    this.scheduleCancelFabReset();
  }

  onComprobanteChange(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0] || null;
    this.comprobanteFile = file;
  }

  setTipoPedido(tipo: TipoPedido): void {
    this.tipoPedido = tipo;
    if (tipo === 'DOMICILIO' && (this.gpsLat == null || this.gpsLng == null)) {
      this.obtenerGps();
    }
  }

  obtenerGps(): void {
    this.gpsEstado = 'loading';
    this.gpsError = '';
    this.gpsWarning = '';
    this.cdr.detectChanges();

    if (!window.isSecureContext) {
      this.gpsEstado = 'error';
      this.gpsError = 'Activa HTTPS (o localhost) para que el navegador pida GPS real.';
      return;
    }

    if (!('geolocation' in navigator)) {
      this.gpsEstado = 'error';
      this.gpsError = 'Tu dispositivo no soporta geolocalizacion.';
      this.cdr.detectChanges();
      return;
    }

    navigator.geolocation.getCurrentPosition(
      (position) => {
        const accuracy = Math.round(position.coords.accuracy || 0);
        this.gpsAccuracy = accuracy;

        if (accuracy > this.maxGpsAccuracyMeters) {
          this.gpsLat = null;
          this.gpsLng = null;
          this.gpsEstado = 'error';
          this.gpsError = `Ubicacion poco precisa (+/- ${accuracy} m). Acercate a una ventana o sal al exterior y vuelve a capturar.`;
          this.cdr.detectChanges();
          return;
        }

        this.gpsLat = position.coords.latitude;
        this.gpsLng = position.coords.longitude;
        this.gpsEstado = 'ok';
        this.gpsError = '';
        this.gpsWarning =
          accuracy > this.preferredGpsAccuracyMeters
            ? `Ubicacion aproximada (+/- ${accuracy} m). Si puedes, vuelve a capturar cerca de una ventana para mejorarla.`
            : '';
        this.cdr.detectChanges();
      },
      (error) => {
        this.gpsLat = null;
        this.gpsLng = null;
        this.gpsAccuracy = null;
        this.gpsEstado = 'error';
        this.gpsWarning = '';
        if (error.code === error.PERMISSION_DENIED) {
          this.gpsError = 'Debes permitir ubicacion y encender el GPS del telefono.';
        } else if (error.code === error.POSITION_UNAVAILABLE) {
          this.gpsError = 'Ubicacion no disponible. Verifica que el GPS este encendido.';
        } else if (error.code === error.TIMEOUT) {
          this.gpsError = 'Tiempo agotado al pedir ubicacion. Intenta nuevamente.';
        } else {
          this.gpsError = error.message || 'No se pudo obtener la ubicacion';
        }
        this.cdr.detectChanges();
      },
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
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

    if (this.tipoPedido === 'DOMICILIO' && (this.gpsLat == null || this.gpsLng == null)) {
      this.formError = 'Para delivery debes activar GPS y permitir ubicacion.';
      this.obtenerGps();
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
      direccion: '',
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
        this.gpsLat = null;
        this.gpsLng = null;
        this.gpsAccuracy = null;
        this.gpsEstado = 'none';
        this.gpsError = '';
        this.gpsWarning = '';
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

  private resetFabTouch(): void {
    this.fabTouchStartX = null;
    this.fabTouchStartY = null;
  }

  private scheduleCancelFabReset(): void {
    this.clearCancelFabTimer();
    if (!this.cancelFabMode) return;
    this.resetCancelFabTimer = window.setTimeout(() => {
      this.cancelFabMode = false;
      this.cdr.detectChanges();
    }, 4000);
  }

  private clearCancelFabTimer(): void {
    if (this.resetCancelFabTimer == null) return;
    window.clearTimeout(this.resetCancelFabTimer);
    this.resetCancelFabTimer = null;
  }

}
