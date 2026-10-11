from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from polycast.short_cycle import collect_short
from polycast.store import database


class DatabaseNetworkLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.db'

    def write_from_another_connection(self, key):
        # A 100ms second writer exposes a held lock without a 30s test delay.
        with closing(sqlite3.connect(self.path, timeout=.1)) as other:
            with other:
                other.execute('INSERT INTO meta(key,value) VALUES(?,?)', (key, 'verified'))

    def test_initialized_reader_does_not_block_an_independent_writer(self):
        with database(self.path) as conn:
            self.assertFalse(conn.in_transaction)
            self.write_from_another_connection('parallel_reader')
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='parallel_reader'").fetchone()[0],
                             'verified')

    def test_business_transaction_still_rolls_back_on_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'caller failed'):
            with database(self.path) as conn:
                conn.execute("INSERT INTO meta(key,value) VALUES('business_write','uncommitted')")
                raise RuntimeError('caller failed')
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM meta WHERE key='business_write'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM meta WHERE key='evidence_digest_v3'").fetchone())

    def test_collection_releases_metadata_writer_before_network_wait(self):
        with database(self.path) as conn:
            conn.execute("INSERT INTO meta(key,value) VALUES('prior_metadata','pending')")
            def slow_snapshot(market, now):
                self.write_from_another_connection('during_network')
                return None
            with patch('polycast.short_cycle._mlb_snapshot', side_effect=slow_snapshot), patch(
                    'polycast.short_cycle._twap_snapshot', return_value=None):
                result = collect_short(conn, [{'id': 'test', 'category': 'daily'}])
            self.assertEqual(result['errors'], [])
            self.assertEqual(result['new_versions'], 0)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM meta WHERE key='during_network'").fetchone())

    def test_failed_initialization_closes_its_connection(self):
        conn = Mock()
        with patch('polycast.store.sqlite3.connect', return_value=conn), patch(
                'polycast.store._initialize_database', side_effect=sqlite3.OperationalError('busy')):
            with self.assertRaises(sqlite3.OperationalError), database(self.path):
                self.fail('Initialization should not yield')
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()
