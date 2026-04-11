from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pos.infrastructure.offline import (
    OfflineRetentionError,
    destroy_unreplayed_segment_after_usb_export,
    export_segment_bundle_to_usb,
    purge_replayed_segment_with_receipt,
)


class Command(BaseCommand):
    help = 'Exporta o purga segmentos offline con receipts tamper-evident.'

    def add_arguments(self, parser):
        parser.add_argument('root_dir', help='Directorio raiz del journal offline.')
        parser.add_argument('segment_id', help='Segmento objetivo.')
        parser.add_argument(
            '--stream',
            default='sales',
            help='Prefijo semantico del stream. Default: sales.',
        )
        parser.add_argument(
            '--action',
            choices=('export_usb', 'purge_synced', 'purge_after_usb'),
            required=True,
            help='Accion operativa de retencion.',
        )
        parser.add_argument('--usb-root', help='Directorio montado del USB para export manual.')
        parser.add_argument('--actor', default='manager', help='Actor responsable del override/export.')
        parser.add_argument('--reason', default='', help='Motivo operativo.')
        parser.add_argument(
            '--server-replay-receipt',
            default='',
            help='Receipt remoto obligatorio para purge_synced.',
        )
        parser.add_argument(
            '--usb-export-receipt-signature',
            default='',
            help='Firma del usb_export_receipt previo para purge_after_usb.',
        )
        parser.add_argument(
            '--manager-override',
            action='store_true',
            help='Confirma override explicito de gerente para purge_after_usb sobre segmento no sincronizado.',
        )
        parser.add_argument('--json', action='store_true', help='Salida estructurada en JSON.')

    def handle(self, *args, **options):
        root_dir = Path(options['root_dir'])
        segment_id = options['segment_id']
        stream_name = options['stream']
        action = options['action']
        actor = options['actor']
        reason = options['reason']
        receipt_secret = str(
            getattr(settings, 'OFFLINE_JOURNAL_RECEIPT_SECRET', '') or getattr(settings, 'SECRET_KEY', '')
        ).strip()

        try:
            if action == 'export_usb':
                usb_root = str(options.get('usb_root') or '').strip()
                if not usb_root:
                    raise CommandError('--usb-root es requerido para export_usb')
                payload = export_segment_bundle_to_usb(
                    root_dir=root_dir,
                    stream_name=stream_name,
                    segment_id=segment_id,
                    usb_root=Path(usb_root),
                    actor=actor,
                    reason=reason,
                    receipt_secret=receipt_secret,
                )
            elif action == 'purge_synced':
                payload = purge_replayed_segment_with_receipt(
                    root_dir=root_dir,
                    stream_name=stream_name,
                    segment_id=segment_id,
                    actor=actor,
                    reason=reason,
                    server_replay_receipt=options.get('server_replay_receipt') or '',
                    receipt_secret=receipt_secret,
                )
            else:
                if not bool(options.get('manager_override')):
                    raise CommandError(
                        '--manager-override es requerido para purge_after_usb de segmento no sincronizado'
                    )
                payload = destroy_unreplayed_segment_after_usb_export(
                    root_dir=root_dir,
                    stream_name=stream_name,
                    segment_id=segment_id,
                    actor=actor,
                    reason=reason,
                    usb_export_receipt_signature=options.get('usb_export_receipt_signature') or '',
                    receipt_secret=receipt_secret,
                    manager_override=True,
                )
        except OfflineRetentionError as exc:
            raise CommandError(str(exc)) from exc

        if options.get('json'):
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=True))
            return

        self.stdout.write(f"receipt_type={payload.get('receipt_type')}")
        self.stdout.write(f"segment_id={payload.get('segment_id')}")
        self.stdout.write(f"created_at={payload.get('created_at')}")
        self.stdout.write(f"signature={payload.get('signature')}")
