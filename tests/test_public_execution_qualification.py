from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import public_execution_episode as original
from experiments import public_execution_qualification as qualification
from tests import test_public_execution_episode as helpers


def fixture(shares='20'):
    expected, gamma, clob, clock, clock_receipts, initial, _, later = helpers.fixture()
    for items in (initial, later):
        for i in (3, 4):
            items[i] = helpers.mutate_body(items[i], lambda body: [
                row.update(size='100') for rows in (body['asks'], body['bids']) for row in rows])
    def improve_down(body):
        body['asks'][0]['price'] = '.38'
        body['bids'][0]['price'] = '.37'
    later[4] = helpers.mutate_body(later[4], improve_down)
    packet = original.initial_packet(expected, clock, initial, Decimal(shares), 250)
    frozen = original.freeze(packet, helpers.START + 120500, helpers.MONO + 120500000000)
    return frozen, later, clock_receipts


def shift_receipt(receipt, shift, *, source_shift=True):
    result = deepcopy(receipt)
    for key in ('sent_local_ms', 'received_local_ms'):
        result[key] += shift
    for key in ('sent_monotonic_ns', 'received_monotonic_ns'):
        result[key] += shift * 1000000
    body = json.loads(result['raw_text'])
    if source_shift and isinstance(body, dict) and 'timestamp' in body:
        body['timestamp'] = str(int(body['timestamp']) + shift)
        result['raw_text'] = original.encoded(body)
        result['raw_sha256'] = original.sha(result['raw_text'])
    return result


class PublicExecutionQualificationTests(unittest.TestCase):
    def test_both_alternative_snapshots_can_pass_without_exchange_or_profit_credit(self):
        frozen, later, _ = fixture()
        before = deepcopy((frozen, later))
        result = qualification.qualify(frozen, later)
        self.assertTrue(result['both_alternative_snapshots_qualified'])
        self.assertFalse(result['minimum_unit_conflict_resolved'])
        for key in ('live_ready', 'exchange_fills_verified', 'exchange_order_eligibility_verified',
                    'actual_fee_verified', 'profitability_credit', 'prediction_credit'):
            self.assertFalse(result[key])
        self.assertEqual((frozen, later), before)

    def test_quote_fresh_at_receipt_can_expire_before_initial_seal(self):
        frozen, later, _ = fixture()
        packet = json.loads(frozen['packet_json'])
        frozen = original.freeze(packet, helpers.START + 126500, helpers.MONO + 126500000000)
        later = [shift_receipt(item, 6000) for item in later]
        self.assertEqual(original.replay(frozen, later)['results']['UP']['status'], 'snapshot_fillable')
        result = qualification.qualify(frozen, later)
        self.assertFalse(result['results']['UP']['qualified'])
        self.assertIn('initial_seal: book is not proven fresh at decision', result['results']['UP']['reasons'])

    def test_first_later_book_can_expire_before_second_book_arrives(self):
        frozen, later, _ = fixture()
        later[-1] = shift_receipt(later[-1], 6000)
        self.assertEqual(original.replay(frozen, later)['results']['UP']['status'], 'snapshot_fillable')
        result = qualification.qualify(frozen, later)
        up = result['results']['UP']
        self.assertFalse(up['qualified'])
        self.assertGreater(up['later_shared_decision_age']['receipt_age_ms'], 5000)
        self.assertIn('later_shared_decision: book is not proven fresh at decision', up['reasons'])

    def test_five_shares_are_reported_separately_from_five_dollars(self):
        frozen, later, _ = fixture('5')
        result = qualification.qualify(frozen, later)
        for side in ('UP', 'DOWN'):
            audit = result['results'][side]['initial_minimum_scenarios']
            self.assertTrue(audit['documented_book_shares']['met'])
            self.assertFalse(audit['documented_gamma_notional']['met'])
            self.assertFalse(result['results'][side]['qualified'])

    def test_price_improvement_can_drop_actual_gross_below_notional_scenario(self):
        frozen, later, _ = fixture('10')
        result = qualification.qualify(frozen, later)
        up = result['results']['UP']
        self.assertTrue(up['initial_minimum_scenarios']['both_scenarios_met'])
        later[3] = helpers.mutate_body(later[3], lambda body: (
            body['asks'][0].update(price='.45'), body['bids'][0].update(price='.44')))
        up = qualification.qualify(frozen, later)['results']['UP']
        self.assertFalse(up['later_minimum_scenarios']['both_scenarios_met'])
        self.assertFalse(up['qualified'])

    def test_uncertain_future_receipt_is_not_rescued_by_later_decision(self):
        frozen, later, _ = fixture()
        packet = json.loads(frozen['packet_json'])
        initial = packet['initial_receipts']
        initial[3] = helpers.mutate_body(initial[3], lambda body: body.update(
            timestamp=str(initial[3]['received_local_ms'])))
        packet = original.initial_packet(packet['expected'], packet['clock'], initial, Decimal('20'), 250)
        frozen = original.freeze(packet, helpers.START + 121000, helpers.MONO + 121000000000)
        later = [shift_receipt(item, 1000) for item in later]
        result = qualification.qualify(frozen, later)
        self.assertFalse(result['results']['UP']['qualified'])
        self.assertTrue(any('Original initial quote unavailable' in reason
                            for reason in result['results']['UP']['reasons']))
        self.assertTrue(result['original_receipt_freshness_rules_unchanged'])

    def test_source_and_receipt_ages_are_not_interchangeable(self):
        frozen, later, _ = fixture()
        packet = json.loads(frozen['packet_json'])
        r = packet['initial_receipts'][3]
        body = json.loads(r['raw_text'])
        body['timestamp'] = str(r['received_local_ms'] - 6000)
        audit = qualification.decision_age(body, r, packet['clock'],
            r['received_local_ms'] + 20, r['received_monotonic_ns'] + 20000000)
        self.assertTrue(audit['receipt_fresh'])
        self.assertFalse(audit['source_fresh'])

    def test_decision_before_receipt_and_clock_step_are_rejected(self):
        frozen, later, _ = fixture()
        packet = json.loads(frozen['packet_json'])
        r = later[3]
        body = json.loads(r['raw_text'])
        for local, mono in ((r['received_local_ms'] - 1, r['received_monotonic_ns']),
                            (r['received_local_ms'] + 1000, r['received_monotonic_ns'])):
            with self.assertRaises(ValueError):
                qualification.decision_age(body, r, packet['clock'], local, mono)

    def test_new_capture_claims_directory_and_sources_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection = root / 'selection.json'
            selection.write_text('{"markets":[]}', encoding='utf-8')
            def unavailable(source, output, shares, hold):
                self.assertTrue((root / 'episode/started.json').is_file())
                self.assertTrue((root / 'episode/source-bundle.json').is_file())
                self.assertEqual(source, root / 'episode/selection-frozen.json')
                output.mkdir()
                original.write_new(output / 'capture.json', {'status': 'capture_unavailable'})
                return {'reason': 'No active market'}
            with patch.object(original, 'capture', side_effect=unavailable) as mock:
                result = qualification.capture(selection, root / 'episode', Decimal('20'), 250)
                self.assertEqual(result['status'], 'capture_unavailable')
                with self.assertRaises(FileExistsError):
                    qualification.capture(selection, root / 'episode', Decimal('20'), 250)
                self.assertEqual(mock.call_count, 1)

    def test_captured_source_text_preserves_exact_original_bytes(self):
        for name, item in qualification.source_bundle().items():
            self.assertEqual(item['text'].encode('utf-8'), (qualification.ROOT / name).read_bytes())
            self.assertEqual(original.sha(item['text']), item['sha256'])


if __name__ == '__main__':
    unittest.main()
