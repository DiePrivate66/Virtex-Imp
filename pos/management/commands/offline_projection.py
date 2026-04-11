from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from pos.infrastructure.offline import (
    OfflineProjectionConfig,
    OfflineProjectionError,
    get_projection_status,
    rebuild_projection_in_place,
)


class Command(BaseCommand):
    help = 'Inspecciona o reconstruye projection.sqlite del journal offline.'

    def add_arguments(self, parser):
        parser.add_argument('root_dir', help='Directorio raiz donde viven los segmentos offline.')
        parser.add_argument(
            '--stream',
            default='sales',
            help='Prefijo semantico del stream. Default: sales.',
        )
        parser.add_argument(
            '--window-hours',
            type=int,
            default=24,
            help='Ventana caliente de proyeccion. Default: 24.',
        )
        parser.add_argument(
            '--rebuild',
            action='store_true',
            help='Reconstuye projection.sqlite in-place.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Salida estructurada en JSON.',
        )

    def handle(self, *args, **options):
        config = OfflineProjectionConfig(
            root_dir=Path(options['root_dir']),
            stream_name=options['stream'],
            window_hours=options['window_hours'],
        )
        try:
            if options.get('rebuild'):
                payload = rebuild_projection_in_place(config=config)
            else:
                payload = get_projection_status(config=config)
        except OfflineProjectionError as exc:
            raise CommandError(str(exc)) from exc

        if options.get('json'):
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=True))
            return

        self.stdout.write(f"mode={payload.get('mode')}")
        self.stdout.write(f"db_path={payload.get('db_path')}")
        self.stdout.write(f"window_hours={payload.get('window_hours')}")
        self.stdout.write(f"row_count={payload.get('row_count')}")
        self.stdout.write(f"last_segment_id={payload.get('last_segment_id') or 'none'}")
        self.stdout.write(f"last_event_id={payload.get('last_event_id') or 'none'}")
        self.stdout.write(f"projected_at={payload.get('projected_at') or 'n/a'}")
        self.stdout.write(f"disk_free_bytes={payload.get('disk_free_bytes')}")
        if payload.get('reason'):
            self.stdout.write(f"reason={payload.get('reason')}")
