from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from pos.ledger_registry import MIN_SUPPORTED_QUEUE_SCHEMA, REGISTRY_VERSION, get_registry_hash
from pos.models import LedgerRegistryActivation


class Command(BaseCommand):
    help = 'Sincroniza el semaforo runtime LedgerRegistryActivation con el registry actual.'

    def add_arguments(self, parser):
        parser.add_argument('--maintenance-mode', choices=['on', 'off'])
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args, **options):
        activation = LedgerRegistryActivation.get_solo()
        activation.active_registry_version = REGISTRY_VERSION
        activation.active_registry_hash = get_registry_hash()
        activation.min_supported_queue_schema = MIN_SUPPORTED_QUEUE_SCHEMA
        if options.get('maintenance_mode') == 'on':
            activation.maintenance_mode = True
        elif options.get('maintenance_mode') == 'off':
            activation.maintenance_mode = False
        activation.activated_at = timezone.now()
        activation.save()

        payload = {
            'singleton_key': activation.singleton_key,
            'active_registry_version': activation.active_registry_version,
            'active_registry_hash': activation.active_registry_hash,
            'min_supported_queue_schema': activation.min_supported_queue_schema,
            'maintenance_mode': activation.maintenance_mode,
            'activated_at': activation.activated_at.isoformat(),
        }
        if options.get('as_json'):
            self.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2))
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Ledger registry activado: {activation.active_registry_version} {activation.active_registry_hash[:12]}"
            )
        )
