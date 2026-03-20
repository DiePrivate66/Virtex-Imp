import json
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pos.models import Categoria, Producto


class Command(BaseCommand):
    help = "Sincroniza categorias y productos desde un archivo JSON de menu."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            default=str(Path("pos") / "data" / "menu_seed.json"),
            help="Ruta del archivo JSON a importar.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Elimina productos y categorias que no existan en el seed.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        file_path = Path(options["file"])
        if not file_path.exists():
            raise CommandError(f"No existe el archivo de seed: {file_path}")

        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"JSON invalido en {file_path}: {exc}") from exc

        if not isinstance(payload, list):
            raise CommandError("El seed debe ser una lista de categorias.")

        seed_category_names = set()
        seed_product_names = set()
        created_categories = 0
        updated_categories = 0
        created_products = 0
        updated_products = 0

        for category_data in payload:
            category_name = (category_data.get("categoria") or "").strip()
            if not category_name:
                raise CommandError("Cada categoria debe tener un nombre no vacio.")

            icono = (category_data.get("icono") or "").strip()
            productos = category_data.get("productos") or []
            if not isinstance(productos, list):
                raise CommandError(f"La categoria {category_name} tiene productos invalidos.")

            categoria, created = Categoria.objects.get_or_create(
                nombre=category_name,
                defaults={"icono": icono},
            )
            seed_category_names.add(category_name)

            category_changed = False
            if categoria.icono != icono:
                categoria.icono = icono
                category_changed = True

            if created:
                created_categories += 1
            elif category_changed:
                categoria.save(update_fields=["icono"])
                updated_categories += 1

            for product_data in productos:
                product_name = (product_data.get("nombre") or "").strip()
                if not product_name:
                    raise CommandError(f"La categoria {category_name} tiene un producto sin nombre.")

                try:
                    price = Decimal(str(product_data["precio"]))
                except Exception as exc:
                    raise CommandError(
                        f"Precio invalido para {product_name} en categoria {category_name}."
                    ) from exc

                active = bool(product_data.get("activo", True))
                seed_product_names.add(product_name)

                producto, created = Producto.objects.get_or_create(
                    nombre=product_name,
                    defaults={
                        "categoria": categoria,
                        "precio": price,
                        "activo": active,
                    },
                )

                changed_fields = []
                if producto.categoria_id != categoria.id:
                    producto.categoria = categoria
                    changed_fields.append("categoria")
                if producto.precio != price:
                    producto.precio = price
                    changed_fields.append("precio")
                if producto.activo != active:
                    producto.activo = active
                    changed_fields.append("activo")

                if created:
                    created_products += 1
                elif changed_fields:
                    producto.save(update_fields=changed_fields)
                    updated_products += 1

        pruned_products = 0
        pruned_categories = 0
        if options["prune"]:
            product_qs = Producto.objects.exclude(nombre__in=seed_product_names)
            pruned_products = product_qs.count()
            product_qs.delete()

            category_qs = Categoria.objects.exclude(nombre__in=seed_category_names)
            pruned_categories = category_qs.count()
            category_qs.delete()

        self.stdout.write(self.style.SUCCESS("Sincronizacion completada."))
        self.stdout.write(
            f"Categorias creadas: {created_categories}, actualizadas: {updated_categories}, "
            f"eliminadas: {pruned_categories}"
        )
        self.stdout.write(
            f"Productos creados: {created_products}, actualizados: {updated_products}, "
            f"eliminados: {pruned_products}"
        )
