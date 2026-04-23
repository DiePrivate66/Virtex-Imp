import { Routes } from '@angular/router';
import { CustomerMenuComponent } from './pages/customer-menu/customer-menu';
import { LegalPageComponent } from './pages/legal-page/legal-page';
import { OrderConfirmationComponent } from './pages/order-confirmation/order-confirmation';

export const routes: Routes = [
  { path: '', component: CustomerMenuComponent },
  { path: 'privacy', component: LegalPageComponent, data: { page: 'privacy' } },
  { path: 'terms', component: LegalPageComponent, data: { page: 'terms' } },
  { path: 'data-deletion', component: LegalPageComponent, data: { page: 'dataDeletion' } },
  { path: 'delete-data', component: LegalPageComponent, data: { page: 'dataDeletion' } },
  { path: 'confirmacion/:pedidoId', component: OrderConfirmationComponent },
  { path: '**', redirectTo: '' }
];
