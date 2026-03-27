from __future__ import annotations

from dataclasses import dataclass

from pos.models import Inventario, MovimientoInventario, Producto


class InventoryError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class InventoryMovementResult:
    producto_nombre: str
    stock_nuevo: int


def ensure_inventory_for_product(producto: Producto) -> Inventario:
    inventario, _created = Inventario.objects.get_or_create(producto=producto)
    return inventario


def register_inventory_movement(*, producto_id, tipo, cantidad_raw, concepto, registrado_por) -> InventoryMovementResult:
    try:
        producto = Producto.objects.get(id=producto_id)
    except Producto.DoesNotExist as exc:
        raise InventoryError('Producto no encontrado', status_code=404) from exc

    try:
        cantidad = int(cantidad_raw)
    except (TypeError, ValueError) as exc:
        raise InventoryError('La cantidad debe ser un numero entero') from exc

    if cantidad <= 0:
        raise InventoryError('La cantidad debe ser mayor a 0')

    inventario = ensure_inventory_for_product(producto)
    stock_anterior = inventario.stock_actual

    if tipo == 'ENTRADA':
        inventario.stock_actual += cantidad
    elif tipo in ('SALIDA', 'MERMA'):
        inventario.stock_actual -= cantidad
    elif tipo == 'AJUSTE':
        inventario.stock_actual = cantidad
        cantidad = cantidad - stock_anterior
    else:
        raise InventoryError('Tipo de movimiento invalido')

    inventario.save()

    MovimientoInventario.objects.create(
        producto=producto,
        tipo=tipo,
        cantidad=cantidad,
        stock_anterior=stock_anterior,
        stock_nuevo=inventario.stock_actual,
        concepto=concepto or '',
        registrado_por=registrado_por,
    )

    return InventoryMovementResult(
        producto_nombre=producto.nombre,
        stock_nuevo=inventario.stock_actual,
    )


def update_inventory_configuration(*, producto_id, stock_minimo=None, unidad=None) -> Inventario:
    try:
        inventario = Inventario.objects.get(producto_id=producto_id)
    except Inventario.DoesNotExist as exc:
        raise InventoryError('Inventario no encontrado', status_code=404) from exc

    if stock_minimo is not None:
        try:
            inventario.stock_minimo = int(stock_minimo)
        except (TypeError, ValueError) as exc:
            raise InventoryError('El stock minimo debe ser un numero entero') from exc

    if unidad is not None:
        inventario.unidad = unidad

    inventario.save()
    return inventario
