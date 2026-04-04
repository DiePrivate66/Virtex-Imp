from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from pos.infrastructure.offline import (
    JournalIntegrityError,
    SegmentJournal,
    reconcile_snapshot_with_segment,
    recover_segment_prefix,
    reseal_segment_from_snapshot,
)


class SegmentJournalTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base_dir = Path(self.temp_dir.name)
        self.segment_path = self.base_dir / 'sales-20260404-001.jsonl'
        self.snapshot_path = self.base_dir / 'sales-20260404-001.snapshot.json'
        self.journal = SegmentJournal(
            segment_path=self.segment_path,
            snapshot_path=self.snapshot_path,
            segment_id='sales-20260404-001',
        )

    def _read_snapshot(self) -> dict[str, object]:
        return json.loads(self.snapshot_path.read_text(encoding='utf-8'))

    def _read_lines(self) -> list[bytes]:
        return self.segment_path.read_bytes().splitlines(keepends=True)

    def test_append_updates_snapshot_and_hash_chain(self):
        first = self.journal.append_event(
            event_id='evt-1',
            payload={'sale_total': '10.00'},
            client_transaction_id='tx-1',
            queue_session_id='queue-a',
            session_seq_no=1,
            summary={'total_sales': 1, 'amount_total': '10.00'},
        )
        second = self.journal.append_event(
            event_id='evt-2',
            payload={'sale_total': '12.50'},
            client_transaction_id='tx-2',
            queue_session_id='queue-a',
            session_seq_no=2,
        )

        snapshot = self._read_snapshot()
        recovery = self.journal.recover()

        self.assertEqual(second['prev_record_hash'], first['record_hash'])
        self.assertEqual(snapshot['record_count'], 2)
        self.assertEqual(snapshot['last_event_id'], 'evt-2')
        self.assertEqual(snapshot['last_record_hash'], second['record_hash'])
        self.assertEqual(snapshot['rolling_crc32'], recovery.rolling_crc32)
        self.assertEqual(snapshot['summary'], {'total_sales': 1, 'amount_total': '10.00'})
        self.assertNotEqual(snapshot['rolling_crc32'], '00000000')

    def test_recovery_stops_at_truncated_tail(self):
        self.journal.append_event(
            event_id='evt-1',
            payload={'sale_total': '10.00'},
        )
        self.journal.append_event(
            event_id='evt-2',
            payload={'sale_total': '12.50'},
        )

        with self.segment_path.open('ab') as handle:
            handle.write(b'{"kind":"event"')

        recovery = recover_segment_prefix(self.segment_path)

        self.assertEqual(recovery.record_count, 2)
        self.assertTrue(recovery.truncated_tail)
        self.assertFalse(recovery.corrupted_tail)
        self.assertEqual(recovery.error_message, 'truncated tail')

    def test_reconcile_repairs_lagging_snapshot(self):
        self.journal.append_event(
            event_id='evt-1',
            payload={'sale_total': '10.00'},
        )
        self.journal.append_event(
            event_id='evt-2',
            payload={'sale_total': '12.50'},
        )

        lagging_snapshot = self._read_snapshot()
        lagging_snapshot.update(
            {
                'record_count': 0,
                'last_offset_confirmed': 0,
                'last_event_id': '',
                'last_record_hash': '',
                'rolling_crc32': '00000000',
            }
        )
        self.snapshot_path.write_text(json.dumps(lagging_snapshot), encoding='utf-8')

        reconciled = reconcile_snapshot_with_segment(self.segment_path, self.snapshot_path)

        self.assertEqual(reconciled['record_count'], 2)
        self.assertEqual(reconciled['last_event_id'], 'evt-2')
        self.assertEqual(reconciled['last_offset_confirmed'], self.segment_path.stat().st_size)

    def test_reconcile_rejects_snapshot_that_claims_missing_confirmed_data(self):
        self.journal.append_event(
            event_id='evt-1',
            payload={'sale_total': '10.00'},
        )
        self.journal.append_event(
            event_id='evt-2',
            payload={'sale_total': '12.50'},
        )

        first_line = self._read_lines()[0]
        self.segment_path.write_bytes(first_line)

        with self.assertRaises(JournalIntegrityError):
            reconcile_snapshot_with_segment(self.segment_path, self.snapshot_path)

    def test_reseal_writes_footer_from_pending_snapshot(self):
        self.journal.append_event(
            event_id='evt-1',
            payload={'sale_total': '10.00'},
        )
        self.journal.append_event(
            event_id='evt-2',
            payload={'sale_total': '12.50'},
        )

        prepared = self.journal.prepare_seal(summary={'total_sales': 2})
        resealed = reseal_segment_from_snapshot(self.segment_path, self.snapshot_path)
        snapshot = self._read_snapshot()
        recovery = recover_segment_prefix(self.segment_path)

        self.assertTrue(prepared['seal_pending'])
        self.assertTrue(resealed)
        self.assertTrue(snapshot['sealed'])
        self.assertFalse(snapshot['seal_pending'])
        self.assertEqual(snapshot['summary'], {'total_sales': 2})
        self.assertIsNotNone(recovery.footer)
        self.assertEqual(recovery.footer['segment_crc32'], recovery.rolling_crc32)

    def test_deleting_middle_record_breaks_hash_chain(self):
        self.journal.append_event(
            event_id='evt-1',
            payload={'sale_total': '10.00'},
        )
        self.journal.append_event(
            event_id='evt-2',
            payload={'sale_total': '12.50'},
        )
        self.journal.append_event(
            event_id='evt-3',
            payload={'sale_total': '15.00'},
        )

        first_line, _, third_line = self._read_lines()
        self.segment_path.write_bytes(first_line + third_line)

        recovery = recover_segment_prefix(self.segment_path)

        self.assertEqual(recovery.record_count, 1)
        self.assertTrue(recovery.corrupted_tail)
        self.assertIn('prev_record_hash mismatch', recovery.error_message)


class OfflineJournalCommandTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base_dir = Path(self.temp_dir.name)
        self.segment_path = self.base_dir / 'sales-20260404-002.jsonl'
        self.snapshot_path = self.base_dir / 'sales-20260404-002.snapshot.json'
        self.journal = SegmentJournal(
            segment_path=self.segment_path,
            snapshot_path=self.snapshot_path,
            segment_id='sales-20260404-002',
        )

    def test_command_outputs_json_status(self):
        self.journal.append_event(
            event_id='evt-command-1',
            payload={'sale_total': '9.99'},
        )

        out = StringIO()
        call_command(
            'offline_journal',
            str(self.segment_path),
            str(self.snapshot_path),
            '--json',
            stdout=out,
        )
        payload = json.loads(out.getvalue())

        self.assertEqual(payload['record_count'], 1)
        self.assertFalse(payload['footer_present'])
        self.assertFalse(payload['truncated_tail'])
        self.assertEqual(payload['last_event_id'], 'evt-command-1')

    def test_command_reconcile_repairs_snapshot(self):
        self.journal.append_event(
            event_id='evt-command-1',
            payload={'sale_total': '9.99'},
        )
        self.journal.append_event(
            event_id='evt-command-2',
            payload={'sale_total': '4.25'},
        )

        snapshot = json.loads(self.snapshot_path.read_text(encoding='utf-8'))
        snapshot['record_count'] = 0
        snapshot['last_offset_confirmed'] = 0
        snapshot['last_event_id'] = ''
        snapshot['last_record_hash'] = ''
        snapshot['rolling_crc32'] = '00000000'
        self.snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')

        out = StringIO()
        call_command(
            'offline_journal',
            str(self.segment_path),
            str(self.snapshot_path),
            '--reconcile',
            '--json',
            stdout=out,
        )
        payload = json.loads(out.getvalue())

        self.assertTrue(payload['reconciled'])
        self.assertEqual(payload['record_count'], 2)
        self.assertEqual(payload['last_event_id'], 'evt-command-2')

    def test_command_reseal_writes_pending_footer(self):
        self.journal.append_event(
            event_id='evt-command-1',
            payload={'sale_total': '9.99'},
        )
        self.journal.prepare_seal()

        out = StringIO()
        call_command(
            'offline_journal',
            str(self.segment_path),
            str(self.snapshot_path),
            '--reseal',
            '--json',
            stdout=out,
        )
        payload = json.loads(out.getvalue())

        self.assertTrue(payload['resealed'])
        self.assertTrue(payload['sealed'])
        self.assertTrue(payload['footer_present'])

    def test_command_strict_fails_on_truncated_tail(self):
        self.journal.append_event(
            event_id='evt-command-1',
            payload={'sale_total': '9.99'},
        )
        with self.segment_path.open('ab') as handle:
            handle.write(b'{"kind":"event"')

        with self.assertRaises(CommandError):
            call_command(
                'offline_journal',
                str(self.segment_path),
                str(self.snapshot_path),
                '--strict',
                stdout=StringIO(),
            )
