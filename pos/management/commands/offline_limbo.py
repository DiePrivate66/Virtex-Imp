from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from pos.infrastructure.offline import OfflineJournalRuntimeConfig, SegmentedJournalRuntime


class Command(BaseCommand):
    help = 'Inspecciona el snapshot de limbo actual desde un directorio de journal offline.'

    def add_arguments(self, parser):
        parser.add_argument('root_dir', help='Directorio raiz donde viven los segmentos JSONL y sidecars.')
        parser.add_argument(
            '--stream',
            default='sales',
            help='Prefijo semantico del stream. Default: sales.',
        )
        parser.add_argument(
            '--segment-max-bytes',
            type=int,
            default=100 * 1024 * 1024,
            help='Tamano maximo por segmento para la runtime local.',
        )
        parser.add_argument(
            '--recent-limit',
            type=int,
            default=50,
            help='Cantidad de ventas recientes que conserva el summary de limbo.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Salida estructurada en JSON.',
        )

    def handle(self, *args, **options):
        runtime = SegmentedJournalRuntime(
            config=OfflineJournalRuntimeConfig(
                root_dir=Path(options['root_dir']),
                stream_name=options['stream'],
                segment_max_bytes=options['segment_max_bytes'],
                limbo_recent_limit=options['recent_limit'],
                sidecar_max_bytes=getattr(settings, 'OFFLINE_JOURNAL_SIDECAR_MAX_BYTES', 64 * 1024),
                projection_window_hours=getattr(settings, 'OFFLINE_JOURNAL_PROJECTION_WINDOW_HOURS', 24),
            )
        )
        payload = runtime.get_limbo_view()

        if options.get('json'):
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=True))
            return

        self.stdout.write(f"stream={payload['stream_name']}")
        self.stdout.write(f"segment_id={payload['segment_id'] or 'none'}")
        self.stdout.write(f"segment_path={payload['segment_path'] or 'n/a'}")
        self.stdout.write(f"snapshot_path={payload['snapshot_path'] or 'n/a'}")
        self.stdout.write(f"record_count={payload['record_count']}")
        self.stdout.write(f"sealed={payload['sealed']}")
        self.stdout.write(f"mode={payload.get('mode', 'journal_only')}")
        summary = payload.get('summary') or {}
        self.stdout.write(
            f"total_sales={summary.get('total_sales', 0)} amount_total={summary.get('amount_total', '0.00')}"
        )
        projection = payload.get('projection') or {}
        self.stdout.write(
            f"projection_available={projection.get('available', False)} "
            f"projection_rows={projection.get('row_count', 0)} "
            f"projection_window={projection.get('window_hours', 0)}h"
        )
        self.stdout.write(
            f"verify_path_available={payload.get('verify_path_available', False)} "
            f"last_verify_status={payload.get('last_verify_status', 'unknown')}"
        )
        recent_sales = summary.get('recent_sales') or []
        self.stdout.write(f'recent_sales={len(recent_sales)}')
