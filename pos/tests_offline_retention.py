from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.core.management.base import CommandError

from pos.infrastructure.offline import (
    OfflineJournalRuntimeConfig,
    SegmentedJournalRuntime,
    export_segment_bundle_to_usb,
)


class OfflineRetentionTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root_dir = Path(self.temp_dir.name) / 'journal'
        self.usb_dir = Path(self.temp_dir.name) / 'usb'
        self.runtime = SegmentedJournalRuntime(
            config=OfflineJournalRuntimeConfig(root_dir=self.root_dir, stream_name='sales')
        )
        self.runtime.append_sale_event(
            event_id='evt-sale-1',
            payload={'sale_total': '12.00', 'payment_status': 'PAID'},
            client_transaction_id='tx-1',
        )
        self.segment_id = self.runtime.get_limbo_view()['segment_id']

    @override_settings(OFFLINE_JOURNAL_RECEIPT_SECRET='test-secret')
    def test_export_segment_bundle_to_usb_writes_signed_receipt(self):
        receipt = export_segment_bundle_to_usb(
            root_dir=self.root_dir,
            stream_name='sales',
            segment_id=self.segment_id,
            usb_root=self.usb_dir,
            actor='manager',
            reason='disk panic',
            receipt_secret='test-secret',
        )

        self.assertEqual(receipt['receipt_type'], 'usb_export_receipt')
        self.assertTrue((self.usb_dir / 'sales' / self.segment_id / f'{self.segment_id}.jsonl').exists())
        self.assertTrue(receipt['signature'])

    @override_settings(OFFLINE_JOURNAL_RECEIPT_SECRET='test-secret')
    def test_command_purge_synced_removes_segment_after_receipt(self):
        self.runtime.seal_active_segment()
        out = StringIO()
        call_command(
            'offline_retention',
            str(self.root_dir),
            self.segment_id,
            '--stream',
            'sales',
            '--action',
            'purge_synced',
            '--actor',
            'manager',
            '--reason',
            'disk pressure',
            '--server-replay-receipt',
            'srv-ack-1',
            '--json',
            stdout=out,
        )
        payload = json.loads(out.getvalue())

        self.assertEqual(payload['receipt_type'], 'purge_receipt')
        self.assertFalse((self.root_dir / f'{self.segment_id}.jsonl').exists())
        self.assertFalse((self.root_dir / f'{self.segment_id}.snapshot.json').exists())

    @override_settings(OFFLINE_JOURNAL_RECEIPT_SECRET='test-secret')
    def test_command_purge_after_usb_requires_manager_override(self):
        export_out = StringIO()
        call_command(
            'offline_retention',
            str(self.root_dir),
            self.segment_id,
            '--stream',
            'sales',
            '--action',
            'export_usb',
            '--usb-root',
            str(self.usb_dir),
            '--actor',
            'manager',
            '--reason',
            'manual backup',
            '--json',
            stdout=export_out,
        )
        export_payload = json.loads(export_out.getvalue())

        with self.assertRaises(CommandError):
            call_command(
                'offline_retention',
                str(self.root_dir),
                self.segment_id,
                '--stream',
                'sales',
                '--action',
                'purge_after_usb',
                '--actor',
                'manager',
                '--reason',
                'manual override',
                '--usb-export-receipt-signature',
                export_payload['signature'],
                stdout=StringIO(),
            )

        self.assertTrue((self.root_dir / f'{self.segment_id}.jsonl').exists())

    @override_settings(OFFLINE_JOURNAL_RECEIPT_SECRET='test-secret')
    def test_command_purge_after_usb_requires_prior_export_signature(self):
        export_out = StringIO()
        call_command(
            'offline_retention',
            str(self.root_dir),
            self.segment_id,
            '--stream',
            'sales',
            '--action',
            'export_usb',
            '--usb-root',
            str(self.usb_dir),
            '--actor',
            'manager',
            '--reason',
            'manual backup',
            '--json',
            stdout=export_out,
        )
        export_payload = json.loads(export_out.getvalue())

        purge_out = StringIO()
        call_command(
            'offline_retention',
            str(self.root_dir),
            self.segment_id,
            '--stream',
            'sales',
            '--action',
            'purge_after_usb',
            '--actor',
            'manager',
            '--reason',
            'manual override',
            '--manager-override',
            '--usb-export-receipt-signature',
            export_payload['signature'],
            '--json',
            stdout=purge_out,
        )
        purge_payload = json.loads(purge_out.getvalue())

        self.assertEqual(purge_payload['purge_mode'], 'manual_usb_override')
        self.assertTrue(purge_payload['manager_override_confirmed'])
        self.assertEqual(purge_payload['override_actor'], 'manager')
        self.assertFalse((self.root_dir / f'{self.segment_id}.jsonl').exists())
