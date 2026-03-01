import { Routes } from '@angular/router';
import { CustomerMenuComponent } from './pages/customer-menu/customer-menu';
import { OrderConfirmationComponent } from './pages/order-confirmation/order-confirmation';

export const routes: Routes = [
  { path: '', component: CustomerMenuComponent },
  { path: 'confirmacion/:pedidoId', component: OrderConfirmationComponent },
  { path: '**', redirectTo: '' }
];