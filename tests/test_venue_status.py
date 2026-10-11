from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polycast.short_cycle import collect_short
from polycast.store import database, evidence_for_market, insert_evidence, _upsert_source
from polycast.venue_status import COINBASE_STATUS, coinbase_status_evidence


class VenueStatusTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads((Path(__file__).parent/'fixtures/coinbase-exchange-status.json').read_text(encoding='utf-8'))
        self.now = datetime(2026, 10, 3, 8, 6, tzinfo=timezone.utc)
        self.market = {'id':'test', 'category':'crypto5m', 'source_ref':'BTCUSD:1791014700'}

    def test_actual_exchange_states_are_quality_context_with_separate_observation_time(self):
        item = coinbase_status_evidence(self.payload, self.market, self.now)
        self.assertEqual(item['published_at'], self.now.isoformat(timespec='microseconds'))
        self.assertIn('REST API: degraded_performance', item['summary'])
        self.assertIn('BTC-USD: operational', item['summary'])
        self.assertIn('not a directional price signal', item['summary'])
        self.assertIn(self.payload['page']['updated_at'], item['summary'])
        self.assertNotIn('incident_updates', item['summary'])
        self.assertEqual(item['origin_story_id'], self.market['source_ref'])

    def test_consumer_status_page_cannot_stand_in_for_exchange_status(self):
        self.payload['page']['id'] = 'kr0djjh0jyy9'
        self.payload['page']['name'] = 'Coinbase'
        with self.assertRaisesRegex(ValueError, 'Wrong Coinbase Exchange'):
            coinbase_status_evidence(self.payload, self.market, self.now)

    def test_missing_duplicate_unknown_and_misidentified_components_are_not_healthy(self):
        cases = []
        missing = copy.deepcopy(self.payload); missing['components'].pop(); cases.append(missing)
        duplicate = copy.deepcopy(self.payload); duplicate['components'].append(duplicate['components'][0]); cases.append(duplicate)
        unknown = copy.deepcopy(self.payload); unknown['components'][0]['status']='unknown'; cases.append(unknown)
        wrong = copy.deepcopy(self.payload); wrong['components'][0]['name']='Ethereum'; cases.append(wrong)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                coinbase_status_evidence(payload, self.market, self.now)

    def test_future_change_time_and_naive_observation_are_rejected(self):
        self.payload['page']['updated_at']=(self.now+timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError, 'future'):
            coinbase_status_evidence(self.payload, self.market, self.now)
        with self.assertRaises(ValueError):
            coinbase_status_evidence(self.payload, self.market, self.now.replace(tzinfo=None))

    def test_price_failure_does_not_discard_rtds_and_status_failure_does_not_discard_prices(self):
        item=coinbase_status_evidence(self.payload,self.market,self.now)
        item.update(source='RTDS',source_id='chainlink_btc_twap60',feed_url='https://data.chain.link/streams/btc-usd')
        with tempfile.TemporaryDirectory() as folder, database(Path(folder)/'test.db') as conn:
            with patch('polycast.short_cycle._btc_5m_snapshot',side_effect=RuntimeError('price unavailable')), patch(
                    'polycast.short_cycle._twap_snapshot',return_value=item), patch(
                    'polycast.short_cycle.fetch_bytes',side_effect=RuntimeError('status unavailable')) as network:
                result=collect_short(conn,[self.market])
            self.assertEqual(result['fetched'],1)
            self.assertEqual(result['new_versions'],1)
            self.assertEqual(len(result['errors']),2)
            self.assertEqual(network.call_args.kwargs['timeout'],3)
            self.assertEqual(conn.execute('SELECT source_id FROM evidence').fetchone()[0],'chainlink_btc_twap60')

    def test_status_requested_once_without_holding_sqlite_writer_lock(self):
        import sqlite3
        item=coinbase_status_evidence(self.payload,self.market,self.now)
        item.update(source='RTDS',source_id='chainlink_btc_twap60',feed_url='https://data.chain.link/streams/btc-usd')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.db'
            with database(path) as conn:
                def response(*args,**kwargs):
                    with closing(sqlite3.connect(path,timeout=.1)) as other:
                        with other:
                            other.execute("INSERT INTO meta VALUES('status_read_parallel_writer','ok')")
                    return json.dumps(self.payload).encode()
                with patch('polycast.short_cycle._btc_5m_snapshot',return_value=None), patch(
                        'polycast.short_cycle._twap_snapshot',return_value=item), patch(
                        'polycast.short_cycle.fetch_bytes',side_effect=response) as network:
                    result=collect_short(conn,[self.market,{**self.market,'id':'second'}])
                self.assertEqual(network.call_count,1)
                self.assertEqual(result['fetched'],4)
                self.assertEqual(result['errors'],[])

    def test_status_context_is_never_collected_without_market_price_data(self):
        with tempfile.TemporaryDirectory() as folder, database(Path(folder)/'test.db') as conn:
            with patch('polycast.short_cycle._btc_5m_snapshot',return_value=None), patch(
                    'polycast.short_cycle._twap_snapshot',return_value=None), patch(
                    'polycast.short_cycle.fetch_bytes') as network:
                result=collect_short(conn,[self.market])
            network.assert_not_called()
            self.assertEqual(result['fetched'],0)

    def test_status_selection_enforces_observation_cutoff_freshness_and_venue_identity(self):
        with tempfile.TemporaryDirectory() as folder, database(Path(folder)/'test.db') as conn:
            _upsert_source(conn,'coinbase_exchange_status','Coinbase Exchange service status','official',
                           COINBASE_STATUS,'official_source',COINBASE_STATUS,'',1)
            item=coinbase_status_evidence(self.payload,self.market,self.now)
            insert_evidence(conn,[item])
            captured=(self.now+timedelta(seconds=1)).isoformat()
            conn.execute('UPDATE evidence SET fetched_at=?',(captured,))
            conn.execute('UPDATE evidence SET first_seen_at=?',(captured,))
            conn.execute('UPDATE evidence_current SET activated_at=?',(captured,))
            self.assertEqual(len(evidence_for_market(conn,self.market,as_of=self.now.isoformat())),0)
            self.assertEqual(len(evidence_for_market(conn,self.market,as_of=captured)),1)
            self.assertEqual(len(evidence_for_market(conn,self.market,as_of=(self.now+timedelta(seconds=121)).isoformat())),0)
            other={**self.market,'source_ref':'BTCUSD:other'}
            self.assertEqual(len(evidence_for_market(conn,other,as_of=captured)),0)
            conn.execute("UPDATE source_registry SET feed_url='https://status.coinbase.com/api/v2/summary.json'")
            self.assertEqual(len(evidence_for_market(conn,self.market,as_of=captured)),0)


if __name__=='__main__':
    unittest.main()
