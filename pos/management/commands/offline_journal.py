from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from pos.infrastructure.offline import (
    JournalIntegrityError,
    reconcile_snapshot_with_segment,
    recover_segment_prefix,
    reseal_segment_from_snapshot,
)


class Command(BaseCommand):
    help = 'Inspecciona, reconcilia o re-sella un segmento JSONL offline y su sidecar .snapshot.'

    def add_arguments(self, parser):
        parser.add_argument('segment_path', help='Ruta del segmento JSONL.')
        parser.add_argument('snapshot_path', help='Ruta del sidecar .snapshot.')
        parser.add_argument(
            '--segment-id',
            help='Identificador semantico del segmento cuando el snapshot aun no existe.',
        )
        parser.add_argument(
            '--reconcile',
            action='store_true',
            help='Repara el sidecar desde el journal antes de emitir estado.',
        )
        parser.add_argument(
            '--reseal',
            action='store_true',
            help='Intenta re-sellar el segmento si existe footer pendiente en el sidecar.',
        )
        parser.add_argument(
            '--strict',
            action='store_true',
            help='Falla si existe cola truncada/corrupta o inconsistencia de integridad.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Salida estructurada en JSON.',
        )

    def handle(self, *args, **options):
        segment_path = Path(options['segment_path'])
        snapshot_path = Path(options['snapshot_path'])
        segment_id = options.get('segment_id')
        reconcile = bool(options.get('reconcile'))
        reseal = bool(options.get('reseal'))
        strict = bool(options.get('strict'))
        as_json = bool(options.get('json'))

        try:
            if reconcile or reseal:
                snapshot = reconcile_snapshot_with_segment(
                    segment_path,
                    snapshot_path,
                    segment_id=segment_id,
                )
            else:
                snapshot = None

            resealed = False
            if reseal:
                resealed = reseal_segment_from_snapshot(segment_path, snapshot_path)
                snapshot = reconcile_snapshot_with_segment(
                    segment_path,
                    snapshot_path,
                    segment_id=segment_id,
                )

            recovery = recover_segment_prefix(segment_path)
        except JournalIntegrityError as exc:
            raise CommandError(str(exc)) from exc

        snapshot = snapshot or reconcile_snapshot_with_segment(
            segment_path,
            snapshot_path,
            segment_id=segment_id,
        )

        payload = {
            'segment_path': str(segment_path),
            'snapshot_path': str(snapshot_path),
            'segment_exists': segment_path.exists(),
            'snapshot_exists': snapshot_path.exists(),
            'record_count': recovery.record_count,
            'last_valid_offset': recovery.last_valid_offset,
            'last_event_id': recovery.last_event_id,
            'last_record_hash': recovery.last_record_hash,
            'rolling_crc32': recovery.rolling_crc32,
            'footer_present': recovery.footer is not None,
            'truncated_tail': recovery.truncated_tail,
            'corrupted_tail': recovery.corrupted_tail,
            'error_message': recovery.error_message,
            'sealed': bool(snapshot.get('sealed')),
            'seal_pending': bool(snapshot.get('seal_pending')),
            'reconciled': reconcile or reseal,
            'resealed': resealed,
            'summary': snapshot.get('summary', {}),
        }

        unhealthy = recovery.truncated_tail or recovery.corrupted_tail
        if strict and unhealthy:
            raise CommandError(recovery.error_message or 'offline journal health check failed')

        if as_json:
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=True))
            return

        self.stdout.write(f"segment={payload['segment_path']}")
        self.stdout.write(f"snapshot={payload['snapshot_path']}")
        self.stdout.write(
            f"records={payload['record_count']} offset={payload['last_valid_offset']} "
            f"sealed={payload['sealed']} footer={payload['footer_present']}"
        )
        self.stdout.write(
            f"tail truncated={payload['truncated_tail']} corrupted={payload['corrupted_tail']}"
        )
        if payload['last_event_id']:
            self.stdout.write(f"last_event_id={payload['last_event_id']}")
        if payload['reconciled'] or payload['resealed']:
            self.stdout.write(
                f"reconciled={payload['reconciled']} resealed={payload['resealed']}"
            )
        if payload['error_message']:
            self.stdout.write(f"detail={payload['error_message']}")
