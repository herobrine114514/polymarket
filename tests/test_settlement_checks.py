from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polycast.config import Config
from polycast.dashboard_state import snapshot
from polycast.short_cycle import CHAINLINK_BTC_TWAP, reconcile_short
from polycast.store import database, upsert_markets


class SettlementCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(data_dir=Path(self.temp.name))
        start = datetime.fromtimestamp(int(datetime.now(timezone.utc).timestamp() // 300 * 300), timezone.utc) - timedelta(minutes=40)
        end = start + timedelta(minutes=5)
        self.market = {'id': '1', 'event_id': 'event-1', 'event_title': 'BTC',
            'event_slug': 'btc-updown-5m-' + str(int(start.timestamp())),
            'question': 'BTC up/down', 'description': 'Exact Chainlink TWAP contract',
            'category': 'crypto5m', 'end_at': end.isoformat(),
            'source_ref': 'BTCUSD:' + str(int(start.timestamp())),
            'yes_label': 'Up', 'no_label': 'Down', 'yes_token': 'up', 'no_token': 'down',
            'liquidity': 9000, 'volume_24h': 1000, 'min_order_size': 5, 'tick_size': .01, 'fee_rate': .07}
        self.remote = {'id': '1', 'closed': True, 'description': self.market['description'],
            'outcomes': '["Up","Down"]', 'clobTokenIds': '["up","down"]',
            'endDate': end.isoformat(), 'resolutionSource': CHAINLINK_BTC_TWAP,
            'cryptoMarketConfig': {'id': 'btc-5m-twap-60', 'twapLookbackSeconds': 60},
            'closedTime': (end + timedelta(minutes=3)).isoformat(), 'outcomePrices': '["1","0"]'}
        self.event = {'id': 'event-1', 'closed': True, 'markets': [self.remote]}
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            prediction = conn.execute('''INSERT INTO predictions
                (market_id,as_of,rules_hash,evidence_hash,probability,abstain,reasoning,
                 assumptions_json,supports_json,counters_json,model_output_json)
                VALUES(?,?,?,?,?,0,'test','[]','[]','[]','{}')''',
                ('1', (start + timedelta(minutes=1)).isoformat(), 'rules', 'evidence', .78)).lastrowid
            conn.execute('''INSERT INTO paper_orders
                (prediction_id,market_id,event_id,risk_group,side,created_at,shares,avg_price,
                 fee,total_cost,expected_edge,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (prediction, '1', 'event-1', 'btc-5m', 'NO',
                 (start + timedelta(minutes=1, seconds=5)).isoformat(), 5, .05, .01663, .26663, .11, 'open'))

    def reconcile(self, relay):
        with database(self.config.database) as conn, patch(
                'polycast.short_cycle.fetch_json', return_value=self.event), patch(
                'polycast.short_cycle._twap_winner', return_value=relay):
            return reconcile_short(conn, categories=('crypto5m',))

    def test_missing_or_conflicting_boundary_cannot_settle_an_order(self):
        for relay, reason in ((None, 'twap_boundary_missing'), ('NO', 'source_conflict')):
            with self.subTest(reason=reason):
                result = self.reconcile(relay)
                self.assertEqual(result['pending'], ['1'])
                self.assertEqual(result['settled'], [])
                with closing(sqlite3.connect(self.config.database)) as conn:
                    self.assertEqual(conn.execute('SELECT status,settlement FROM paper_orders').fetchone(), ('open', None))
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM resolutions').fetchone()[0], 0)
                    self.assertEqual(conn.execute('SELECT status,reason,official_side FROM settlement_checks').fetchone(),
                                     ('pending', reason, 'YES'))
        with patch('polycast.dashboard_state.scheduled_task', return_value={'available': False}):
            state = snapshot(self.config)
        self.assertEqual(state['orders'][0]['settlement_check_reason'], 'source_conflict')
        self.assertEqual(state['orders'][0]['official_winning_side'], 'YES')
        self.assertEqual(state['validation']['completed'], 0)

    def test_matching_source_replaces_pending_check_and_settles_once(self):
        self.reconcile(None)
        result = self.reconcile('YES')
        self.assertEqual(len(result['settled']), 1)
        self.assertEqual(result['settled'][0]['paper_orders_settled'], 1)
        with closing(sqlite3.connect(self.config.database)) as conn:
            self.assertEqual(conn.execute('SELECT status,settlement FROM paper_orders').fetchone(), ('settled', 0.0))
            self.assertEqual(conn.execute('SELECT status,reason FROM settlement_checks').fetchone(), ('verified', 'sources_agree'))
        self.assertEqual(self.reconcile('YES')['settled'], [])

    def test_network_error_is_visible_and_cannot_overwrite_the_ledger(self):
        with database(self.config.database) as conn, patch(
                'polycast.short_cycle.fetch_json', side_effect=RuntimeError('timeout')):
            result = reconcile_short(conn, categories=('crypto5m',))
            self.assertEqual(len(result['errors']), 1)
            self.assertEqual(tuple(conn.execute('SELECT status,reason,official_side FROM settlement_checks').fetchone()),
                             ('error', 'external_data_error', None))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM resolutions').fetchone()[0], 0)

    def test_check_write_is_committed_before_fetching_another_market(self):
        second = {**self.market, 'id': '2', 'event_id': 'event-2'}
        with database(self.config.database) as conn:
            upsert_markets(conn, [second], reset_watchlist=False)
            calls = 0
            def fetch(url):
                nonlocal calls
                calls += 1
                if calls == 2:
                    with closing(sqlite3.connect(self.config.database, timeout=.1)) as other:
                        with other:
                            other.execute("INSERT INTO meta(key,value) VALUES('between_checks','ok')")
                event = json.loads(json.dumps(self.event))
                current = self.market if calls == 1 else second
                event['id'] = current['event_id']; event['markets'][0]['id'] = current['id']
                return event
            with patch('polycast.short_cycle.fetch_json', side_effect=fetch), patch(
                    'polycast.short_cycle._twap_winner', return_value=None):
                result = reconcile_short(conn, categories=('crypto5m',))
            self.assertEqual(len(result['pending']), 2)
            self.assertEqual(result['errors'], [])
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='between_checks'").fetchone()[0], 'ok')
