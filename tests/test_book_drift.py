from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polycast.book_drift import assess_book_drift
from polycast.config import Config
from polycast.execution import FEE_METHOD, fill_totals
from polycast.paper import candidate, _validated_order
from polycast.paper_execution import PaperExecutionError, sample_fok_execution
from polycast.store import database, save_prediction, upsert_markets
from tests.test_delayed_execution import Clock, book, info, live, market


class ForecastBookDriftTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime.now(timezone.utc)
        self.config = Config()
        self.basis = {'prediction_id': 1, 'forecast_as_of': self.at.isoformat(),
                      'captured_at': (self.at-timedelta(milliseconds=100)).isoformat(),
                      'book_timestamp': (self.at-timedelta(milliseconds=200)).isoformat(),
                      'yes_bid': .59, 'yes_ask': .61, 'p_yes_mid': .6}

    def test_direction_conversion_uses_no_mid_as_complement(self):
        audit = assess_book_drift(self.basis, {'side': 'NO', 'book_bid': .39, 'book_ask': .41},
                                  self.config, now=self.at+timedelta(seconds=1))
        self.assertTrue(audit['allowed'])
        self.assertAlmostEqual(audit['current_implied_yes_mid'], .6)

    def test_cheaper_ask_is_not_an_edge_when_forecast_book_has_moved(self):
        audit = assess_book_drift(self.basis, {'side': 'YES', 'book_bid': .29, 'book_ask': .31},
                                  self.config, now=self.at+timedelta(seconds=1))
        self.assertFalse(audit['allowed'])
        self.assertEqual(audit['status'], 'drifted')
        self.assertAlmostEqual(audit['midpoint_change'], -.3)

    def test_fresh_execution_book_does_not_refresh_original_information(self):
        audit = assess_book_drift(self.basis, {'side': 'YES', 'book_bid': .59, 'book_ask': .61},
                                  self.config, now=self.at+timedelta(seconds=31))
        self.assertFalse(audit['allowed'])
        self.assertEqual(audit['status'], 'expired')

    def test_missing_or_postforecast_basis_cannot_pass(self):
        quote = {'side': 'YES', 'book_bid': .59, 'book_ask': .61}
        for basis in (None, {**self.basis, 'captured_at': (self.at+timedelta(seconds=1)).isoformat()},
                      {**self.basis, 'p_yes_mid': .9}):
            with self.subTest(basis=basis):
                self.assertFalse(assess_book_drift(basis, quote, self.config, now=self.at)['allowed'])

    def test_crossed_nan_and_one_sided_books_fail_closed(self):
        for bid, ask in ((.7,.6), (None,.6), (.5,float('nan')), (.5,float('inf'))):
            with self.subTest(bid=bid, ask=ask):
                self.assertFalse(assess_book_drift(self.basis,
                    {'side':'YES', 'book_bid':bid, 'book_ask':ask},self.config,now=self.at)['allowed'])


class BookDriftIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Config(data_dir=Path(temp.name))
        self.market = {**market(), 'category': 'crypto5m'}
        self.at = datetime.now(timezone.utc)
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            self.pid = save_prediction(conn, '1', 'rules', 'evidence', {
                'as_of': self.at.isoformat(), 'p_yes': .8, 'abstain': False,
                'review': {'approved': True}, 'reviewed_at': self.at.isoformat(),
                'valid_until': self.market['end_at']})
            conn.execute('''INSERT INTO market_baselines
                (prediction_id,captured_at,book_timestamp,yes_best_bid,yes_best_ask,p_yes_mid)
                VALUES(?,?,?,?,?,?)''', (self.pid, self.at.isoformat(), self.at.isoformat(), .59,.61,.6))
        self.basis = {'prediction_id': self.pid, 'forecast_as_of': self.at.isoformat(),
                      'captured_at': self.at.isoformat(), 'book_timestamp': self.at.isoformat(),
                      'yes_bid': .59, 'yes_ask': .61, 'p_yes_mid': .6}
        fills = [{'price': .61, 'shares': 5.}]
        gross, avg, fees = fill_totals(fills, 5., self.market['fee_rate'])
        self.frozen = {'market_id':'1', 'event_id':'event', 'risk_group':'fed-policy',
                       'prediction_id':self.pid, 'side':'YES', 'shares':5., 'avg_ask':avg,
                       'worst_ask':.61, 'fills':fills, 'fee_method':FEE_METHOD,
                       'fee_per_share':fees/5, 'total_cost':gross+fees,
                       'expected_edge':.8-avg-fees/5, 'book_bid':.59,'book_ask':.61,
                       'book_timestamp':self.at.isoformat(), 'captured_at':self.at.isoformat(),
                       'forecast_book_basis':self.basis}

    def test_candidate_records_drift_rejections_without_creating_an_order(self):
        quote = {**self.frozen, 'avg_ask': .31, 'worst_ask': .31, 'book_bid': .29,'book_ask':.31}
        patches = [('polycast.paper.active_halts', []), ('polycast.paper._blackout_reason',None),
                   ('polycast.paper._prediction_current',True), ('polycast.paper._joint_event_reason',None),
                   ('polycast.paper._live_matches',True), ('polycast.paper.live_market',{}),
                   ('polycast.paper._quote',quote)]
        for name, value in patches:
            p = patch(name, return_value=value); p.start(); self.addCleanup(p.stop)
        with database(self.config.database) as conn:
            result, reason = candidate(conn, '1', self.config)
            self.assertIsNone(result)
            self.assertIn('forecast basis invalidated',reason)
            audits = conn.execute('SELECT allowed,audit_json FROM forecast_book_checks').fetchall()
            self.assertEqual(len(audits),2)
            self.assertTrue(all(not row['allowed'] for row in audits))
            self.assertEqual(json.loads(audits[0]['audit_json'])['status'],'drifted')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0],0)

    def test_delayed_price_improvement_is_rejected_after_midpoint_change(self):
        changed = {**book([{'price':'.31','size':'10'}]), 'bids':[{'price':'.29','size':'10'}]}
        clock = Clock()
        with patch('polycast.paper_execution.clob_market_info', return_value=info()), \
                patch('polycast.paper_execution.order_book', return_value=changed), \
                patch('polycast.paper_execution.time.monotonic', side_effect=clock.now), \
                patch('polycast.paper_execution.time.sleep', side_effect=clock.sleep):
            with self.assertRaises(PaperExecutionError) as caught:
                sample_fok_execution(self.market,self.frozen,self.config,live(self.market))
        self.assertEqual(caught.exception.audit['forecast_book_check']['status'],'drifted')
        self.assertEqual(caught.exception.audit['external_orders_submitted'],0)

    def test_final_validation_does_not_trust_forged_basis(self):
        frozen = {**self.frozen, 'forecast_book_basis': {**self.basis,'p_yes_mid':.5}}
        with database(self.config.database) as conn, \
                patch('polycast.paper.assert_entry_allowed'), \
                patch('polycast.paper._blackout_reason',return_value=None), \
                patch('polycast.paper._prediction_current',return_value=True), \
                patch('polycast.paper._joint_event_reason',return_value=None):
            with self.assertRaisesRegex(ValueError,'immutable forecast book basis'):
                _validated_order(conn,frozen,self.config)
