import os
import django

# 1. Configurar el entorno de Django para que este script pueda hablar con la Base de Datos
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from pos.models import Categoria, Producto

def cargar_datos():
    print("🚀 Iniciando carga del menú RAMÓN BY BOSCO...")

    # DATOS EXACTOS PROPORCIONADOS
    menu_data = {
        "POLLO": [
            ("ALITAS X8", 10.00),
            ("BONELESS 7 UNIDADES", 7.50),
            ("BONELESS 14 UNIDADES", 14.00),
            ("TENDERS 4 UNIDADES", 8.50),
            ("TENDERS 8 UNIDADES", 16.00),
            ("SANDWICH SIMPLE", 5.00),
            ("SANDWICH DOBLE", 8.00),
            ("WRAP-MON", 7.00),
        ],
        "BEBIDAS": [
            ("AGUA", 1.50),
            ("AGUA CON GAS", 1.50),
            ("GASEOSAS CONVENCIONALES", 1.75),
            ("FUZE TEA", 1.75),
            ("DR. PEPPER", 3.50),
            ("MOUNTAIN DEW", 3.50),
            ("COCA-COLA DE VAINILLA", 3.50),
        ],
        "EXTRAS": [
            ("PAN RAMON", 2.50),
            ("PAPAS FRITAS", 3.00),
            ("EXTRA SALSA DE TEMPORADA", 1.00),
            ("EXTRA SALSA PREMIUM", 1.25),
            ("MOZARELLA STICK", 5.00),
            ("PICKLES X5", 1.00),
            ("JALAPEÑO X5", 1.00),
            ("CHEESE PAPITAS", 1.75),
        ]
    }

    # 2. (Opcional) Limpiar base de datos previa para evitar duplicados
    # Si prefieres no borrar nada, comenta las siguientes 2 líneas:
    print("🧹 Limpiando menú anterior...")
    Producto.objects.all().delete()
    Categoria.objects.all().delete()

    # 3. Crear Categorías y Productos
    for cat_nombre, productos in menu_data.items():
        # Crear la categoría
        categoria_obj = Categoria.objects.create(nombre=cat_nombre)
        print(f"📂 Categoría creada: {cat_nombre}")

        for prod_nombre, precio in productos:
            Producto.objects.create(
                categoria=categoria_obj,
                nombre=prod_nombre,
                precio=precio,
                activo=True
            )
            print(f"   - 🍔 {prod_nombre} (${precio})")

    print("\n✅ ¡MENÚ CARGADO EXITOSAMENTE!")
    print("   Ahora ve a http://127.0.0.1:8000/ y refresca la página.")

if __name__ == '__main__':
    cargar_datos()