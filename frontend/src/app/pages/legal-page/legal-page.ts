import { CommonModule } from '@angular/common';
import { Component, computed, inject } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';

type LegalPageKey = 'privacy' | 'terms' | 'dataDeletion';

type LegalSection = {
  title: string;
  paragraphs?: string[];
  items?: string[];
};

type LegalPage = {
  title: string;
  eyebrow: string;
  intro: string;
  sections: LegalSection[];
};

const LEGAL_PAGES: Record<LegalPageKey, LegalPage> = {
  privacy: {
    title: 'Politica de privacidad',
    eyebrow: 'Privacidad',
    intro:
      'Esta politica aplica al uso de la PWA de pedidos, el POS interno y los canales de comunicacion operados para RAMON by Bosco mediante Virtex Imp.',
    sections: [
      {
        title: 'Datos que recopilamos',
        items: [
          'Nombre, telefono, correo, cedula o RUC cuando el cliente los comparte para realizar pedidos o solicitar comprobantes.',
          'Detalle del pedido, metodo de pago, direccion, ubicacion GPS y referencias necesarias para coordinar entregas.',
          'Mensajes operativos usados para confirmar pedidos, coordinar delivery, registrar pagos y atender soporte.'
        ]
      },
      {
        title: 'Uso de los datos',
        items: [
          'Procesar pedidos, registrar ventas, emitir comprobantes y coordinar la entrega.',
          'Enviar actualizaciones de estado, facturas, tickets o soporte postventa.',
          'Mantener trazabilidad operativa, auditoria, seguridad, conciliacion contable y prevencion de fraude.'
        ]
      },
      {
        title: 'Comparticion de datos',
        paragraphs: [
          'Los datos se usan solo para operar el servicio. Pueden compartirse con proveedores tecnicos necesarios para el funcionamiento del sistema, como infraestructura de hosting, servicios de correo, mensajeria, mapas, impresion o pasarelas de pago.'
        ]
      },
      {
        title: 'Conservacion',
        paragraphs: [
          'Conservamos la informacion durante el tiempo necesario para operar pedidos, cumplir obligaciones contables, resolver incidentes operativos y atender solicitudes legales o de soporte.'
        ]
      },
      {
        title: 'Derechos y contacto',
        paragraphs: [
          'Puedes solicitar acceso, rectificacion o eliminacion de tus datos escribiendo al canal oficial del negocio o usando la pagina de eliminacion de datos.'
        ]
      }
    ]
  },
  terms: {
    title: 'Condiciones del servicio',
    eyebrow: 'Terminos',
    intro:
      'Estas condiciones regulan el uso de la PWA de pedidos, el POS interno y los canales digitales asociados a RAMON by Bosco.',
    sections: [
      {
        title: 'Uso del servicio',
        items: [
          'El cliente debe proporcionar informacion correcta para que el pedido pueda prepararse y entregarse.',
          'La disponibilidad de productos, precios y tiempos de entrega puede cambiar segun operacion, inventario y cobertura.',
          'El negocio puede rechazar o cancelar pedidos con datos incompletos, pago no verificable o imposibilidad de entrega.'
        ]
      },
      {
        title: 'Pagos y comprobantes',
        items: [
          'Los pagos en efectivo o transferencia se validan segun el flujo operativo vigente del negocio.',
          'Los comprobantes se emiten con los datos entregados por el cliente y pueden enviarse por correo cuando el cliente proporciona una direccion valida.'
        ]
      },
      {
        title: 'Entrega',
        paragraphs: [
          'Para pedidos con delivery, el cliente debe compartir una ubicacion o referencia suficiente. El valor de la carrera puede coordinarse directamente con el repartidor cuando aplique.'
        ]
      }
    ]
  },
  dataDeletion: {
    title: 'Eliminacion de datos',
    eyebrow: 'Datos personales',
    intro:
      'Puedes solicitar la eliminacion de datos personales asociados a tus pedidos o conversaciones operativas.',
    sections: [
      {
        title: 'Como solicitarlo',
        items: [
          'Escribe al canal oficial del negocio indicando tu nombre, telefono y el dato que deseas eliminar.',
          'Si usaste correo para recibir comprobantes, incluye ese correo para ubicar la informacion relacionada.',
          'El equipo validara la solicitud antes de ejecutar cambios sobre datos operativos.'
        ]
      },
      {
        title: 'Alcance',
        paragraphs: [
          'Podemos eliminar o anonimizar datos personales cuando no exista una obligacion legal, contable o de seguridad que requiera conservarlos.'
        ]
      },
      {
        title: 'Tiempos',
        paragraphs: [
          'Las solicitudes se atienden en el menor tiempo operativo posible despues de verificar la identidad o titularidad de la informacion.'
        ]
      }
    ]
  }
};

@Component({
  selector: 'app-legal-page',
  imports: [CommonModule, RouterLink],
  templateUrl: './legal-page.html',
  styleUrl: './legal-page.css'
})
export class LegalPageComponent {
  private readonly route = inject(ActivatedRoute);

  readonly page = computed(() => {
    const key = (this.route.snapshot.data['page'] || 'privacy') as LegalPageKey;
    return LEGAL_PAGES[key] || LEGAL_PAGES.privacy;
  });
}
