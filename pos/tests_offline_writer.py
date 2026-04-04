from __future__ import annotations

import io
import json
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from pos.infrastructure.offline import recover_segment_prefix


class OfflineJournalWriterCommandTests(SimpleTestCase):
    def test_offline_writer_appends_sale_event_from_inline_json(self):
        envelope = {
            'event_id': 'writer-sale-001',
            'journal_event_type': 'sale',
            'client_transaction_id': 'writer-sale-001',
            'payload': {
                'sale_total': '15.75',
                'payment_status': 'PAID',
                'journal_capture_source': 'client_runtime_harness',
                'sale_origin': 'POS',
                'display_name': 'Cliente Harness',
            },
        }

        with TemporaryDirectory() as temp_dir:
            out = io.StringIO()
            call_command(
                'offline_writer',
                temp_dir,
                '--stream',
                'sales',
                '--envelope-json',
                json.dumps(envelope),
                '--json',
                stdout=out,
            )
            payload = json.loads(out.getvalue())
            recovery = recover_segment_prefix(Path(payload['segment_path']))

        self.assertEqual(payload['event_id'], 'writer-sale-001')
        self.assertEqual(payload['journal_event_type'], 'sale')
        self.assertEqual(payload['summary']['total_sales'], 1)
        self.assertEqual(payload['summary']['amount_total'], '15.75')
        self.assertEqual(recovery.record_count, 1)
        self.assertEqual(recovery.records[0]['payload']['sale_origin'], 'POS')

    def test_offline_writer_appends_lifecycle_event_from_file(self):
        envelope = {
            'event_id': 'writer-life-001',
            'journal_event_type': 'lifecycle',
            'client_transaction_id': 'writer-life-001',
            'payload': {
                'capture_event_type': 'sale.payment_failed',
                'journal_capture_source': 'client_runtime_harness',
                'sale_origin': 'WEB',
                'failure_reason': 'forced-failure',
            },
        }

        with TemporaryDirectory() as temp_dir:
            with NamedTemporaryFile('w', encoding='utf-8', suffix='.json', delete=False) as handle:
                json.dump(envelope, handle)
                file_path = handle.name
            try:
                out = io.StringIO()
                call_command(
                    'offline_writer',
                    temp_dir,
                    '--stream',
                    'sales',
                    '--envelope-file',
                    file_path,
                    '--json',
                    stdout=out,
                )
                payload = json.loads(out.getvalue())
                recovery = recover_segment_prefix(Path(payload['segment_path']))
            finally:
                Path(file_path).unlink(missing_ok=True)

        self.assertEqual(payload['journal_event_type'], 'lifecycle')
        self.assertEqual(payload['summary']['total_sales'], 0)
        self.assertEqual(payload['summary']['amount_total'], '0.00')
        self.assertEqual(recovery.record_count, 1)
        self.assertEqual(recovery.records[0]['payload']['failure_reason'], 'forced-failure')

    @override_settings(
        OFFLINE_JOURNAL_ROOT='D:/tmp/offline-writer-default',
        OFFLINE_JOURNAL_STREAM_NAME='sales',
    )
    def test_offline_writer_reads_envelope_from_stdin_and_uses_settings_root(self):
        envelope = {
            'event_id': 'writer-stdin-001',
            'journal_event_type': 'sale',
            'payload': {
                'sale_total': '6.00',
                'payment_status': 'PAID',
                'journal_capture_source': 'client_runtime_harness',
                'sale_origin': 'WEB',
            },
        }

        with TemporaryDirectory() as temp_dir:
            with override_settings(OFFLINE_JOURNAL_ROOT=temp_dir):
                out = io.StringIO()
                with patch('sys.stdin', io.StringIO(json.dumps(envelope))):
                    call_command(
                        'offline_writer',
                        '--json',
                        stdout=out,
                    )
                payload = json.loads(out.getvalue())

        self.assertEqual(payload['root_dir'], temp_dir)
        self.assertEqual(payload['summary']['total_sales'], 1)
        self.assertEqual(payload['summary']['amount_total'], '6.00')
