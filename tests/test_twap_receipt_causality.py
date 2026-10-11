from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from polycast.short_cycle import _twap_points, _twap_snapshot, _twap_winner
from polycast.twap_records import decode_twap_record, read_twap_records


class TwapReceiptCausalityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ticks.jsonl'
        self.at = datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)
        self.ms = int(self.at.timestamp() * 1000)

    def record(self, *, price='85000', stamp=None, received=None):
        return {'timestamp_ms': self.ms if stamp is None else stamp,
                'received_at_ms': self.ms + 1000 if received is None else received,
                'full_accuracy_value': str(int(Decimal(price) * 10**18))}

    def write(self, records):
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')

    def test_late_history_is_not_available_at_forecast_cutoff(self):
        self.write([self.record(received=self.ms + 11000)])
        points, audit = read_twap_records(self.path, self.at, self.at + timedelta(seconds=10))
        self.assertEqual(points, [])
        self.assertEqual(audit['not_received_by_cutoff'], 1)
        self.assertEqual(_twap_points(self.path, self.at, self.at + timedelta(seconds=12)),
                         [(self.ms, Decimal('85000'))])

    def test_late_correction_does_not_change_earlier_replay(self):
        self.write([self.record(), self.record(price='84000', received=self.ms + 20000)])
        before = _twap_points(self.path, self.at, self.at + timedelta(seconds=10))
        self.assertEqual(before, [(self.ms, Decimal('85000'))])
        later, audit = read_twap_records(self.path, self.at, self.at + timedelta(seconds=21))
        self.assertEqual(later, [])
        self.assertEqual(audit['conflicting_timestamps'], 1)

    def test_reconnect_duplicate_uses_earliest_known_receipt(self):
        self.write([self.record(received=self.ms + 2000), self.record(received=self.ms + 1000)])
        points, audit = read_twap_records(self.path, self.at, self.at + timedelta(seconds=3))
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]['received_at_ms'], self.ms + 1000)
        self.assertEqual(audit['duplicate_timestamp'], 1)

    def test_unknown_arrival_never_becomes_forecast_input(self):
        item = self.record()
        del item['received_at_ms']
        self.write([item])
        self.assertEqual(_twap_points(self.path, self.at, self.at + timedelta(seconds=3)), [])
        # Boundary audit may describe a legacy observation with unknown timing.
        self.assertIsNone(decode_twap_record(item, require_receipt=False)['received_at_ms'])

    def test_new_decimal_and_old_e18_preserve_exact_price(self):
        price = '85000.123456789012345678'
        legacy = self.record(price=price)
        current = {**legacy, 'feed_protocol': 'polybolt_raw_v1',
                   'price_encoding': 'decimal_usd', 'connection_id': 'connection-1',
                   'channel_seq': 3, 'full_accuracy_value': price}
        self.assertEqual(decode_twap_record(legacy)['price'], Decimal(price))
        self.assertEqual(decode_twap_record(current)['price'], Decimal(price))
        self.write([legacy, current])
        self.assertEqual(len(_twap_points(self.path, self.at, self.at + timedelta(seconds=3))), 1)

    def test_unsupported_or_inconsistent_formats_are_rejected(self):
        legacy = self.record()
        for changes in ({'price_encoding': 'decimal_usd'}, {'feed_protocol': 'unknown'},
                        {'schema_version': 2}, {'schema_version': True},
                        {'timestamp_ms': True}, {'received_at_ms': float(self.ms)},
                        {'window_seconds': 30}, {'quote_currency': 'USDT'},
                        {'price_source': 'coinbase'}, {'symbol': 'ethusd'},
                        {'sequence_contiguous': False}, {'full_accuracy_value': 'NaN'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                decode_twap_record({**legacy, **changes})

    def test_settlement_diagnostic_can_use_late_boundary_received_before_audit(self):
        end = self.at + timedelta(minutes=5)
        end_ms = int(end.timestamp() * 1000)
        market = {'source_ref': f'BTCUSD:{self.ms // 1000}', 'end_at': end.isoformat()}
        self.write([self.record(), self.record(stamp=end_ms, received=end_ms + 7000, price='85010')])
        with patch('polycast.short_cycle.datetime', wraps=datetime) as clock:
            clock.now.return_value = end + timedelta(seconds=8)
            self.assertEqual(_twap_winner(market, self.path), 'YES')
            clock.now.return_value = end + timedelta(seconds=6)
            self.assertIsNone(_twap_winner(market, self.path))

    def test_live_snapshot_cannot_retroactively_use_late_opening_sample(self):
        now = self.at + timedelta(minutes=2)
        market = {'id': 'market', 'category': 'crypto5m',
                  'source_ref': f'BTCUSD:{self.ms // 1000}',
                  'end_at': (self.at + timedelta(minutes=5)).isoformat()}
        self.write([self.record(received=int((now + timedelta(seconds=1)).timestamp()*1000)),
                    self.record(stamp=int((now-timedelta(seconds=2)).timestamp()*1000),
                                received=int((now-timedelta(seconds=1)).timestamp()*1000), price='85010')])
        self.assertIsNone(_twap_snapshot(market, now, self.path))

    def test_naive_cutoff_is_rejected(self):
        with self.assertRaises(ValueError):
            read_twap_records(self.path, self.at.replace(tzinfo=None), self.at)
