import { Injectable, computed, signal } from '@angular/core';
import { CarritoItem, Producto } from '../models/pedido.models';

const STORAGE_KEY = 'bosco_cliente_carrito_v1';

@Injectable({ providedIn: 'root' })
export class CartService {
  private readonly _items = signal<CarritoItem[]>(this.readInitialState());

  readonly items = this._items.asReadonly();
  readonly count = computed(() => this._items().reduce((acc, item) => acc + item.cantidad, 0));
  readonly total = computed(() =>
    this._items().reduce((acc, item) => acc + item.precio * item.cantidad, 0)
  );

  addProduct(producto: Producto): void {
    const items = [...this._items()];
    const existing = items.find((item) => item.id === producto.id && !item.nota);
    if (existing) {
      existing.cantidad += 1;
    } else {
      items.push({
        id: producto.id,
        nombre: producto.nombre,
        precio: producto.precio,
        cantidad: 1,
        nota: ''
      });
    }
    this.update(items);
  }

  increase(index: number): void {
    const items = [...this._items()];
    if (!items[index]) return;
    items[index].cantidad += 1;
    this.update(items);
  }

  decrease(index: number): void {
    const items = [...this._items()];
    if (!items[index]) return;
    items[index].cantidad -= 1;
    if (items[index].cantidad <= 0) {
      items.splice(index, 1);
    }
    this.update(items);
  }

  updateNote(index: number, note: string): void {
    const items = [...this._items()];
    if (!items[index]) return;
    items[index].nota = note.toUpperCase().slice(0, 120);
    this.update(items);
  }

  clear(): void {
    this.update([]);
  }

  private update(items: CarritoItem[]): void {
    this._items.set(items);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
  }

  private readInitialState(): CarritoItem[] {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw) as CarritoItem[];
      if (!Array.isArray(parsed)) return [];
      return parsed.filter((item) => item && item.id && item.cantidad > 0);
    } catch {
      return [];
    }
  }
}