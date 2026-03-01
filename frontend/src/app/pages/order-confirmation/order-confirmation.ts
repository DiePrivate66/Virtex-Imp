import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';

@Component({
  selector: 'app-order-confirmation',
  imports: [CommonModule, RouterLink],
  templateUrl: './order-confirmation.html',
  styleUrl: './order-confirmation.css'
})
export class OrderConfirmationComponent {
  readonly pedidoId: string;

  constructor(route: ActivatedRoute) {
    this.pedidoId = route.snapshot.paramMap.get('pedidoId') || '-';
  }
}