from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from pos.infrastructure.offline import (
    OfflineJournalRuntimeConfig,
    OfflineProjectionConfig,
    OfflineProjectionError,
    SegmentedJournalRuntime,
    get_projection_status,
    rebuild_projection_in_place,
)


class OfflineProjectionTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root_dir = Path(self.temp_dir.name)
        self.runtime = SegmentedJournalRuntime(
            config=OfflineJournalRuntimeConfig(
                root_dir=self.root_dir,
                stream_name='sales',
            )
        )
        self.projection_config = OfflineProjectionConfig(
            root_dir=self.root_dir,
            stream_name='sales',
            window_hours=24,
        )

    def test_projection_status_is_journal_only_when_db_is_absent(self):
        status = get_projection_status(config=self.projection_config)

        self.assertFalse(status['available'])
        self.assertEqual(status['mode'], 'journal_only')
        self.assertIn('projection.sqlite ausente', status['reason'])

    def test_rebuild_projection_in_place_creates_single_projection_sqlite(self):
        self.runtime.append_sale_event(
            event_id='evt-sale-1',
            payload={'sale_total': '10.00', 'payment_status': 'PAID', 'display_name': 'Cliente A'},
            client_transaction_id='tx-1',
        )
        self.runtime.append_lifecycle_event(
            event_id='evt-life-1',
            payload={'journal_event_type': 'lifecycle', 'state': 'boot'},
            client_transaction_id='life-1',
        )
        self.runtime.append_sale_event(
            event_id='evt-sale-2',
            payload={'sale_total': '4.25', 'payment_status': 'PENDING', 'display_name': 'Cliente B'},
            client_transaction_id='tx-2',
        )

        payload = rebuild_projection_in_place(config=self.projection_config)
        view = self.runtime.get_limbo_view()

        self.assertTrue(payload['available'])
        self.assertEqual(payload['row_count'], 2)
        self.assertTrue((self.root_dir / 'sales.projection.sqlite').exists())
        self.assertEqual(view['mode'], 'healthy')
        self.assertTrue(view['projection']['available'])

    def test_rebuild_projection_fails_closed_when_disk_margin_is_missing(self):
        with patch('pos.infrastructure.offline.projection._disk_free_bytes', return_value=0):
            with self.assertRaises(OfflineProjectionError):
                rebuild_projection_in_place(config=self.projection_config)


class OfflineProjectionCommandTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root_dir = Path(self.temp_dir.name)
        runtime = SegmentedJournalRuntime(
            config=OfflineJournalRuntimeConfig(root_dir=self.root_dir, stream_name='sales')
        )
        runtime.append_sale_event(
            event_id='evt-sale-command-1',
            payload={'sale_total': '12.00', 'payment_status': 'PAID'},
            client_transaction_id='tx-command-1',
        )

    def test_command_rebuilds_projection_and_outputs_json(self):
        out = StringIO()
        call_command(
            'offline_projection',
            str(self.root_dir),
            '--stream',
            'sales',
            '--rebuild',
            '--json',
            stdout=out,
        )
        payload = json.loads(out.getvalue())

        self.assertEqual(payload['mode'], 'healthy')
        self.assertEqual(payload['row_count'], 1)

    def test_command_errors_when_rebuild_has_no_disk_margin(self):
        with patch('pos.infrastructure.offline.projection._disk_free_bytes', return_value=0):
            with self.assertRaises(CommandError):
                call_command(
                    'offline_projection',
                    str(self.root_dir),
                    '--stream',
                    'sales',
                    '--rebuild',
                    stdout=StringIO(),
                )
