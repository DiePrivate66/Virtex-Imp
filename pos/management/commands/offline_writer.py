from __future__ import annotations

import json
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pos.infrastructure.offline import (
    OfflineJournalEnvelope,
    OfflineJournalRuntimeConfig,
    append_offline_journal_envelope,
)


class Command(BaseCommand):
    help = 'Apendea un envelope canonico al journal offline usando el mismo writer compartido que usa el shadow capture.'

    def add_arguments(self, parser):
        parser.add_argument(
            'root_dir',
            nargs='?',
            help='Directorio raiz del journal offline. Default: OFFLINE_JOURNAL_ROOT.',
        )
        parser.add_argument(
            '--stream',
            default='',
            help='Prefijo semantico del stream. Default: OFFLINE_JOURNAL_STREAM_NAME o sales.',
        )
        parser.add_argument(
            '--segment-max-bytes',
            type=int,
            default=None,
            help='Tamano maximo por segmento. Default: OFFLINE_JOURNAL_SEGMENT_MAX_BYTES.',
        )
        parser.add_argument(
            '--recent-limit',
            type=int,
            default=None,
            help='Cantidad de ventas recientes que conserva el summary. Default: OFFLINE_JOURNAL_LIMBO_RECENT_LIMIT.',
        )
        parser.add_argument(
            '--envelope-json',
            help='Envelope canonico en JSON inline.',
        )
        parser.add_argument(
            '--envelope-file',
            help='Ruta a un archivo JSON con el envelope canonico.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Salida estructurada en JSON.',
        )

    def handle(self, *args, **options):
        root_dir_value = str(
            options.get('root_dir')
            or getattr(settings, 'OFFLINE_JOURNAL_ROOT', '')
            or ''
        ).strip()
        if not root_dir_value:
            raise CommandError('root_dir requerido o configura OFFLINE_JOURNAL_ROOT')

        envelope = self._load_envelope(options)
        config = OfflineJournalRuntimeConfig(
            root_dir=Path(root_dir_value),
            stream_name=options.get('stream') or getattr(settings, 'OFFLINE_JOURNAL_STREAM_NAME', 'sales'),
            segment_max_bytes=options.get('segment_max_bytes')
            or getattr(settings, 'OFFLINE_JOURNAL_SEGMENT_MAX_BYTES', 100 * 1024 * 1024),
            limbo_recent_limit=options.get('recent_limit')
            or getattr(settings, 'OFFLINE_JOURNAL_LIMBO_RECENT_LIMIT', 50),
        )

        try:
            result = append_offline_journal_envelope(config=config, envelope=envelope)
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        payload = {
            'stream_name': config.stream_name,
            'root_dir': str(config.root_dir),
            'event_id': result['event_id'],
            'journal_event_type': result['journal_event_type'],
            'segment_id': result['limbo'].get('segment_id', ''),
            'segment_path': result['limbo'].get('segment_path', ''),
            'snapshot_path': result['limbo'].get('snapshot_path', ''),
            'record_count': result['limbo'].get('record_count', 0),
            'summary': result['limbo'].get('summary', {}),
        }
        if options.get('json'):
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=True))
            return

        self.stdout.write(f"event_id={payload['event_id']}")
        self.stdout.write(f"journal_event_type={payload['journal_event_type']}")
        self.stdout.write(f"segment_id={payload['segment_id'] or 'none'}")
        self.stdout.write(f"segment_path={payload['segment_path'] or 'n/a'}")
        self.stdout.write(f"record_count={payload['record_count']}")
        summary = payload.get('summary') or {}
        self.stdout.write(
            f"total_sales={summary.get('total_sales', 0)} amount_total={summary.get('amount_total', '0.00')}"
        )

    def _load_envelope(self, options) -> OfflineJournalEnvelope:
        envelope_json = str(options.get('envelope_json') or '').strip()
        envelope_file = str(options.get('envelope_file') or '').strip()
        if envelope_json and envelope_file:
            raise CommandError('Usa solo uno: --envelope-json o --envelope-file')

        if envelope_json:
            raw_payload = envelope_json
        elif envelope_file:
            raw_payload = Path(envelope_file).read_text(encoding='utf-8')
        else:
            raw_payload = sys.stdin.read()

        if not str(raw_payload or '').strip():
            raise CommandError('Envelope vacio')

        try:
            parsed = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise CommandError(f'Envelope JSON invalido: {exc}') from exc

        try:
            return OfflineJournalEnvelope.from_mapping(parsed)
        except Exception as exc:
            raise CommandError(f'Envelope invalido: {exc}') from exc
