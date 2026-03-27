import { CommonModule } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Component, DestroyRef, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { interval } from 'rxjs';
import { startWith, switchMap } from 'rxjs/operators';

import { EstadoPedidoResponse } from '../../models/pedido.models';
import { PedidoApiService } from '../../services/pedido-api';

@Component({
  selector: 'app-order-confirmation',
  imports: [CommonModule, RouterLink],
  templateUrl: './order-confirmation.html',
  styleUrl: './order-confirmation.css'
})
export class OrderConfirmationComponent {
  readonly pedidoId: string;

  status: EstadoPedidoResponse | null = null;
  errorMessage = '';
  receivedMessage = '';
  reportingReceived = false;

  private readonly api = inject(PedidoApiService);
  private readonly destroyRef = inject(DestroyRef);

  constructor(route: ActivatedRoute) {
    this.pedidoId = route.snapshot.paramMap.get('pedidoId') || '-';

    interval(30000)
      .pipe(
        startWith(0),
        switchMap(() => this.api.getEstadoPedido(this.pedidoId)),
        takeUntilDestroyed(this.destroyRef)
      )
      .subscribe({
        next: (status) => {
          this.status = status;
          this.errorMessage = '';
        },
        error: (error: HttpErrorResponse) => {
          this.errorMessage = error.status === 404
            ? 'No pudimos encontrar este pedido.'
            : 'No pudimos actualizar el estado del pedido.';
        }
      });
  }

  get emoji(): string {
    switch (this.status?.estado) {
      case 'PENDIENTE_COTIZACION':
        return '\u{1F4CD}';
      case 'PENDIENTE':
        return '\u23F3';
      case 'COCINA':
        return '\u{1F373}';
      case 'EN_CAMINO':
        return '\u{1F6F5}';
      case 'LISTO':
        return '\u2705';
      default:
        return '?';
    }
  }

  get title(): string {
    switch (this.status?.estado) {
      case 'PENDIENTE_COTIZACION':
        return 'COSTO DE ENVIO EN PROCESO';
      case 'PENDIENTE':
        return 'PEDIDO RECIBIDO';
      case 'COCINA':
        return 'PREPARANDO TU PEDIDO';
      case 'EN_CAMINO':
        return 'PEDIDO EN CAMINO';
      case 'LISTO':
        return 'PEDIDO ENTREGADO';
      default:
        return 'PEDIDO RECIBIDO';
    }
  }

  get subtitle(): string {
    switch (this.status?.estado) {
      case 'PENDIENTE_COTIZACION':
        return 'Estamos confirmando el costo del delivery.';
      case 'PENDIENTE':
        return 'Tu pedido fue enviado correctamente.';
      case 'COCINA':
        return 'Tu comida esta siendo preparada.';
      case 'EN_CAMINO':
        if (this.status?.esperando_confirmacion_delivery) {
          return 'Tu repartidor esta cerrando la entrega final.';
        }
        return 'Tu repartidor ya salio con tu pedido.';
      case 'LISTO':
        return 'Tu pedido fue entregado.';
      default:
        return 'Tu pedido fue enviado correctamente.';
    }
  }

  get etaText(): string | null {
    if (!this.status || this.status.estado !== 'EN_CAMINO') {
      return null;
    }

    const remaining = this.status.minutos_restantes_estimados;
    const original = this.status.tiempo_estimado_minutos;
    const eta = remaining ?? original;
    if (eta == null) {
      return null;
    }

    if (eta <= 0) {
      return 'Llega en cualquier momento';
    }

    return `Llegada estimada en ${eta} min`;
  }

  get totalText(): string {
    return this.status?.total_con_envio ? `$${this.status.total_con_envio}` : '';
  }

  get canReportReceived(): boolean {
    return !!this.status?.puede_reportar_recibido && !this.reportingReceived;
  }

  reportarRecibido(): void {
    if (!this.status || !this.status.puede_reportar_recibido || this.reportingReceived) {
      return;
    }

    this.reportingReceived = true;
    this.receivedMessage = '';
    this.errorMessage = '';

    this.api.reportarPedidoRecibido(this.pedidoId).subscribe({
      next: (resp) => {
        this.reportingReceived = false;
        this.receivedMessage = resp.mensaje;
        if (this.status) {
          this.status = {
            ...this.status,
            cliente_reporto_recibido: true,
            puede_reportar_recibido: false,
            esperando_confirmacion_delivery: true,
          };
        }
      },
      error: (error: HttpErrorResponse) => {
        this.reportingReceived = false;
        this.errorMessage =
          error?.error?.mensaje ||
          'No pudimos registrar que recibiste el pedido.';
      }
    });
  }
}
