from django.core.management.base import BaseCommand

from pos.application.staff.commands import sync_employee_user
from pos.models import Empleado


class Command(BaseCommand):
    help = "Sincroniza los usuarios internos de Django para empleados del POS."

    def handle(self, *args, **options):
        synced = 0
        for empleado in Empleado.objects.all().order_by("id"):
            sync_employee_user(empleado)
            synced += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Sincronizacion completada. Empleados procesados: {synced}"
            )
        )
