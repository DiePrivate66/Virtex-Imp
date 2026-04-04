from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import SimpleTestCase

from pos.infrastructure.offline import (
    OfflineJournalRuntimeConfig,
    SegmentedJournalRuntime,
    load_snapshot_payload,
    persist_snapshot_payload,
)


class SegmentedJournalRuntimeTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root_dir = Path(self.temp_dir.name)

    def test_runtime_updates_limbo_summary_without_counting_lifecycle_events(self):
        runtime = SegmentedJournalRuntime(
            config=OfflineJournalRuntimeConfig(
                root_dir=self.root_dir,
                stream_name='sales',
                limbo_recent_limit=2,
            )
        )

        runtime.append_sale_event(
            event_id='evt-sale-1',
            payload={'total': '10.00', 'payment_status': 'PAID', 'display_name': 'Cliente A'},
            client_transaction_id='tx-1',
            queue_session_id='queue-a',
            session_seq_no=1,
        )
        runtime.append_lifecycle_event(
            event_id='evt-boot-1',
            payload={'system': 'boot'},
            queue_session_id='queue-a',
            session_seq_no=2,
        )
        runtime.append_sale_event(
            event_id='evt-sale-2',
            payload={'sale_total': '4.50', 'payment_status': 'PENDING', 'display_name': 'Cliente B'},
            client_transaction_id='tx-2',
            queue_session_id='queue-a',
            session_seq_no=3,
        )
        runtime.append_sale_event(
            event_id='evt-sale-3',
            payload={'sale_total': '2.25', 'payment_status': 'PAID', 'display_name': 'Cliente C'},
            client_transaction_id='tx-3',
            queue_session_id='queue-a',
            session_seq_no=4,
        )

        payload = runtime.get_limbo_view()
        summary = payload['summary']

        self.assertEqual(summary['total_sales'], 3)
        self.assertEqual(summary['amount_total'], '16.75')
        self.assertEqual(len(summary['recent_sales']), 2)
        self.assertEqual(summary['recent_sales'][0]['event_id'], 'evt-sale-3')
        self.assertEqual(summary['recent_sales'][1]['event_id'], 'evt-sale-2')

    def test_runtime_repairs_lagging_summary_from_journal(self):
        runtime = SegmentedJournalRuntime(
            config=OfflineJournalRuntimeConfig(root_dir=self.root_dir, stream_name='sales')
        )
        runtime.append_sale_event(
            event_id='evt-sale-1',
            payload={'sale_total': '7.00', 'payment_status': 'PAID'},
            client_transaction_id='tx-1',
        )
        runtime.append_sale_event(
            event_id='evt-sale-2',
            payload={'sale_total': '3.25', 'payment_status': 'PAID'},
            client_transaction_id='tx-2',
        )

        view = runtime.get_limbo_view()
        snapshot_path = Path(view['snapshot_path'])
        snapshot = load_snapshot_payload(snapshot_path)
        snapshot['summary'] = {
            'summary_version': 'limbo_sales_v1',
            'total_sales': 0,
            'amount_total': '0.00',
            'recent_sales': [],
        }
        persist_snapshot_payload(snapshot_path, snapshot)

        repaired = runtime.get_limbo_view()

        self.assertEqual(repaired['summary']['total_sales'], 2)
        self.assertEqual(repaired['summary']['amount_total'], '10.25')
        self.assertEqual(repaired['summary']['recent_sales'][0]['event_id'], 'evt-sale-2')

    def test_runtime_rotates_and_seals_segment_when_size_threshold_is_reached(self):
        runtime = SegmentedJournalRuntime(
            config=OfflineJournalRuntimeConfig(
                root_dir=self.root_dir,
                stream_name='sales',
                segment_max_bytes=1024,
            )
        )

        runtime.append_sale_event(
            event_id='evt-sale-1',
            payload={'sale_total': '10.00', 'payment_status': 'PAID', 'display_name': 'X' * 2500},
            client_transaction_id='tx-1',
        )
        first_view = runtime.get_limbo_view()
        runtime.append_sale_event(
            event_id='evt-sale-2',
            payload={'sale_total': '11.00', 'payment_status': 'PAID', 'display_name': 'Y' * 40},
            client_transaction_id='tx-2',
        )
        second_view = runtime.get_limbo_view()

        snapshot_files = sorted(self.root_dir.glob('sales-*.snapshot.json'))

        self.assertEqual(len(snapshot_files), 2)
        self.assertTrue(load_snapshot_payload(snapshot_files[0]).get('sealed'))
        self.assertFalse(load_snapshot_payload(snapshot_files[1]).get('sealed'))
        self.assertNotEqual(first_view['segment_id'], second_view['segment_id'])

    def test_runtime_returns_latest_segment_view_even_after_manual_seal(self):
        runtime = SegmentedJournalRuntime(
            config=OfflineJournalRuntimeConfig(root_dir=self.root_dir, stream_name='sales')
        )
        runtime.append_sale_event(
            event_id='evt-sale-1',
            payload={'sale_total': '6.00', 'payment_status': 'PAID'},
            client_transaction_id='tx-1',
        )

        runtime.seal_active_segment()
        payload = runtime.get_limbo_view()

        self.assertTrue(payload['sealed'])
        self.assertEqual(payload['summary']['total_sales'], 1)


class OfflineLimboCommandTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root_dir = Path(self.temp_dir.name)

    def test_command_outputs_limbo_status_as_json(self):
        runtime = SegmentedJournalRuntime(
            config=OfflineJournalRuntimeConfig(root_dir=self.root_dir, stream_name='sales')
        )
        runtime.append_sale_event(
            event_id='evt-sale-command-1',
            payload={'sale_total': '12.00', 'payment_status': 'PAID'},
            client_transaction_id='tx-command-1',
        )

        out = StringIO()
        call_command(
            'offline_limbo',
            str(self.root_dir),
            '--stream',
            'sales',
            '--json',
            stdout=out,
        )
        payload = json.loads(out.getvalue())

        self.assertEqual(payload['stream_name'], 'sales')
        self.assertEqual(payload['record_count'], 1)
        self.assertEqual(payload['summary']['total_sales'], 1)
        self.assertEqual(payload['summary']['amount_total'], '12.00')
