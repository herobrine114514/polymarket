from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from experiments import public_execution_asof as asof
from experiments import public_execution_episode as base
from tests import test_public_execution_episode as helpers


def point_at(local):
    return {'local_ms': local, 'monotonic_ns': helpers.MONO + (local - helpers.START) * 1000000}


def shift(receipt, milliseconds):
    changed = deepcopy(receipt)
    for key in ('sent_local_ms', 'received_local_ms'):
        changed[key] += milliseconds
    for key in ('sent_monotonic_ns', 'received_monotonic_ns'):
        changed[key] += milliseconds * 1000000
    body = json.loads(changed['raw_text'])
    if isinstance(body, dict) and 'timestamp' in body:
        changed = helpers.mutate_body(changed, lambda b: b.update(timestamp=str(int(b['timestamp']) + milliseconds)))
    return changed


def fixture(shares='20'):
    expected, gamma, clob, clock, clock_receipts, initial, _, later = helpers.fixture()
    for receipts in (initial, later):
        for index in (3, 4):
            receipts[index] = helpers.mutate_body(receipts[index], lambda b: [
                row.update(size='100') for rows in (b['bids'], b['asks']) for row in rows])
    later[4] = helpers.mutate_body(later[4], lambda b: b.update(
        asks=[{'price': '.38', 'size': '100'}], bids=[{'price': '.37', 'size': '100'}]))
    later = [shift(r, 2000) for r in later]
    decision = point_at(initial[-1]['received_local_ms'] + asof.WAIT_MS)
    packet = asof.initial_packet(expected, clock, initial, Decimal(shares), 250, decision)
    frozen = asof.freeze(packet, point_at(decision['local_ms'] + 5))
    later_decision = point_at(later[-1]['received_local_ms'] + asof.WAIT_MS)
    return expected, clock, clock_receipts, initial, frozen, later, later_decision


def refreeze(expected, clock, initial, shares='20'):
    decision = point_at(initial[-1]['received_local_ms'] + asof.WAIT_MS)
    packet = asof.initial_packet(expected, clock, initial, Decimal(shares), 250, decision)
    return asof.freeze(packet, point_at(decision['local_ms'] + 5))


def directory_fixture(directory):
    expected, clock, clocks, initial, frozen, later, decision = fixture()
    receipts = clocks + initial + later
    bundle = {name: {'sha256': base.sha((asof.ROOT / name).read_bytes()),
                     'text': (asof.ROOT / name).read_bytes().decode('utf-8')} for name in asof.SOURCES}
    base.write_new(directory / 'source-bundle.json', bundle)
    base.write_new(directory / 'selection-frozen.json', {'markets': [expected]})
    base.write_new(directory / 'started.json', {'version': asof.VERSION, 'parameters': asof.PARAMETERS,
        'shares': '20', 'hold_ms': 250, 'started': point_at(helpers.START + 118000),
        'selection_frozen_sha256': base.sha((directory / 'selection-frozen.json').read_bytes()),
        'source_bundle_sha256': base.sha((directory / 'source-bundle.json').read_bytes()),
        'source_hashes': {name: item['sha256'] for name, item in bundle.items()}})
    for index, receipt in enumerate(receipts, 1):
        base.write_new(directory / f'receipt-{index:02}.json', receipt)
    result = asof.replay(frozen, later, decision)
    base.write_new(directory / 'frozen-intent.json', frozen)
    base.write_new(directory / 'later-decision.json', decision)
    base.write_new(directory / 'replay.json', result)
    base.write_new(directory / 'capture.json', {'version': asof.VERSION, 'status': 'capture_completed',
        'requests': 12, 'maximum_requests': 12, 'later_request_numbers': list(range(8, 13)),
        'receipt_files': [{'filename': f'receipt-{i:02}.json', 'sha256': base.sha(
            (directory / f'receipt-{i:02}.json').read_bytes())} for i in range(1, 13)], 'summary': result})
    return result


class PublicExecutionAsOfTests(unittest.TestCase):
    def test_separate_new_cohort_can_qualify_ambiguous_receipt_without_modifying_it(self):
        expected, clock, _, initial, _, later, decision = fixture()
        initial[3] = helpers.mutate_body(initial[3], lambda b: b.update(timestamp=str(initial[3]['received_local_ms'])))
        unchanged = deepcopy(initial)
        old = base.initial_packet(expected, clock, initial, Decimal('20'), 250)
        self.assertEqual(old['initial_quotes']['UP']['status'], 'unavailable')
        frozen = refreeze(expected, clock, initial)
        value = asof.replay(frozen, later, decision)
        self.assertEqual(value['results']['UP']['status'], 'asof_snapshot_qualified')
        proof = json.loads(frozen['packet_json'])['initial_quotes']['UP']['quote']['age_proof']
        self.assertEqual(proof['receipt_time_status'], 'interval_ambiguous_at_receipt')
        self.assertGreaterEqual(Decimal(proof['source_age_interval_ms'][0]), 0)
        self.assertEqual(initial, unchanged)
        self.assertFalse(value['old_receipt_time_captures_reclassified'])

    def test_known_future_at_receipt_is_never_rescued_by_fixed_wait(self):
        expected, clock, _, initial, _, later, decision = fixture()
        initial[3] = helpers.mutate_body(initial[3], lambda b: b.update(timestamp=str(initial[3]['received_local_ms'] + 100)))
        frozen = refreeze(expected, clock, initial)
        result = asof.replay(frozen, later, decision)['results']['UP']
        self.assertEqual(result['status'], 'rejected'); self.assertIn('known future', result['reason'])

    def test_full_source_age_interval_remains_bounded_at_later_shared_decision(self):
        *_, frozen, later, decision = fixture()
        later[3] = helpers.mutate_body(later[3], lambda b: b.update(timestamp=str(decision['local_ms'] - 5000)))
        result = asof.replay(frozen, later, decision)['results']['UP']
        self.assertEqual(result['status'], 'rejected'); self.assertIn('Full source/receipt', result['reason'])

    def test_fixed_wait_cannot_be_shortened_extended_or_adapted_until_pass(self):
        expected, clock, _, initial, *_ = fixture()
        for milliseconds in (999, 1251):
            with self.subTest(milliseconds=milliseconds), self.assertRaisesRegex(ValueError, 'Fixed stabilization'):
                asof.initial_packet(expected, clock, initial, Decimal('20'), 250,
                    point_at(initial[-1]['received_local_ms'] + milliseconds))

    def test_stale_earlier_metadata_input_invalidates_common_decision(self):
        expected, clock, _, initial, *_ = fixture()
        initial[0] = shift(initial[0], -6000)
        with self.assertRaisesRegex(ValueError, 'Input receipt stale'):
            refreeze(expected, clock, initial)

    def test_wall_clock_step_between_receipt_and_decision_rejected(self):
        expected, clock, _, initial, *_ = fixture()
        decision = point_at(initial[-1]['received_local_ms'] + 1000)
        decision['local_ms'] += 101
        with self.assertRaisesRegex(ValueError, 'wall-clock step'):
            asof.initial_packet(expected, clock, initial, Decimal('20'), 250, decision)

    def test_clock_width_and_age_are_not_relaxed_by_wait(self):
        expected, clock, _, initial, *_ = fixture()
        for change in ({'uncertainty_ms': 751}, {'checked_at_local_ms': helpers.START - 1}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'verified public clock'):
                refreeze(expected, {**clock, **change}, initial)

    def test_late_sealing_cannot_freeze_an_old_decision(self):
        *_, frozen, later, decision = fixture()
        packet = json.loads(frozen['packet_json'])
        with self.assertRaisesRegex(ValueError, 'seal lag'):
            asof.freeze(packet, point_at(packet['initial_decision']['local_ms'] + 251))

    def test_old_episode_cannot_be_replayed_as_new_protocol(self):
        *_, old, later = helpers.fixture()
        with self.assertRaisesRegex(ValueError, 'Separate as-of'):
            asof.replay(old, later, point_at(later[-1]['received_local_ms'] + 1000))

    def test_frozen_limit_and_complete_depth_preserved(self):
        *_, frozen, later, decision = fixture()
        for asks in ([{'price': '.61', 'size': '100'}], [{'price': '.55', 'size': '19'}]):
            changed = deepcopy(later)
            changed[3] = helpers.mutate_body(changed[3], lambda b: b.update(asks=asks))
            result = asof.replay(frozen, changed, decision)['results']['UP']
            self.assertEqual(result['status'], 'rejected'); self.assertIn('Full FOK depth', result['reason'])

    def test_cost_reserve_applies_even_with_price_within_frozen_limit(self):
        expected, clock, _, initial, _, later, decision = fixture()
        initial[3] = helpers.mutate_body(initial[3], lambda b: b.update(
            asks=[{'price': '.50', 'size': '10'}, {'price': '.60', 'size': '10'}],
            bids=[{'price': '.49', 'size': '100'}]))
        frozen = refreeze(expected, clock, initial)
        later[3] = helpers.mutate_body(later[3], lambda b: b.update(asks=[{'price': '.60', 'size': '100'}]))
        result = asof.replay(frozen, later, decision)['results']['UP']
        self.assertEqual(result['status'], 'rejected'); self.assertIn('frozen reserve', result['reason'])

    def test_per_level_decimal_fee_and_notional_scenario_are_explicit(self):
        *_, frozen, later, decision = fixture()
        later[3] = helpers.mutate_body(later[3], lambda b: b.update(
            asks=[{'price': '.50', 'size': '10'}, {'price': '.55', 'size': '10'}],
            bids=[{'price': '.49', 'size': '100'}]))
        value = asof.replay(frozen, later, decision)['results']['UP']['quote']
        self.assertEqual(value['estimated_fee'], '0.34825')
        self.assertEqual(value['gross_cost'], '10.50')
        self.assertTrue(value['minimum_scenarios']['both_scenarios_met'])
        self.assertFalse(value['minimum_scenarios']['unit_conflict_resolved'])

    def test_passing_share_minimum_does_not_silently_pass_cash_scenario(self):
        *_, frozen, later, decision = fixture('5')
        value = asof.replay(frozen, later, decision)['results']['UP']
        self.assertEqual(value['status'], 'minimum_scenario_rejected')
        self.assertTrue(value['initial_minimum_scenarios']['documented_book_shares']['met'])
        self.assertFalse(value['initial_minimum_scenarios']['documented_gamma_notional']['met'])

    def test_terms_changes_or_wrong_protocol_reject_later_alternatives(self):
        *_, frozen, later, decision = fixture()
        for change in ({'version': 'v2'}, {'closed': True}, {'description': 'Changed rules'}):
            changed = deepcopy(later)
            changed[0] = helpers.mutate_body(changed[0], lambda b: b.update(change))
            self.assertTrue(all(r['status'] == 'rejected' for r in asof.replay(frozen, changed, decision)['results'].values()))

    def test_invalid_geometry_including_zero_size_duplicate_price_rejects(self):
        *_, frozen, later, decision = fixture()
        cases = ([{'price': '.551', 'size': '100'}],
                 [{'price': '.55', 'size': '0'}, {'price': '.55', 'size': '100'}],
                 [{'price': '.54', 'size': '100'}])
        for asks in cases:
            changed = deepcopy(later)
            changed[3] = helpers.mutate_body(changed[3], lambda b: b.update(asks=asks))
            self.assertEqual(asof.replay(frozen, changed, decision)['results']['UP']['status'], 'rejected')

    def test_required_hold_after_seal_cannot_be_skipped(self):
        *_, frozen, later, decision = fixture()
        later = [shift(r, -1150) for r in later]
        with self.assertRaisesRegex(ValueError, 'required sealed hold'):
            asof.replay(frozen, later, point_at(decision['local_ms'] - 1150))

    def test_cutoff_after_fixed_wait_rejects_all_later_alternatives(self):
        expected, clock, _, initial, _, later, decision = fixture()
        expected['end_at_ms'] = decision['local_ms']
        expected['start_at_ms'] = expected['end_at_ms'] - 300000
        end = datetime.fromtimestamp(expected['end_at_ms'] / 1000, timezone.utc).isoformat()
        for receipts in (initial, later):
            receipts[0] = helpers.mutate_body(receipts[0], lambda b: b.update(endDate=end))
            receipts[1] = helpers.mutate_body(receipts[1], lambda b: b['markets'][0].update(endDate=end))
        frozen = refreeze(expected, clock, initial)
        value = asof.replay(frozen, later, decision)
        for result in value['results'].values():
            self.assertEqual(result['status'], 'rejected')
            self.assertIn('outside verified market window', result['reason'])

    def test_auth_or_raw_response_hash_tamper_cannot_produce_quote(self):
        *_, frozen, later, decision = fixture()
        for change in ({'authenticated': True}, {'raw_sha256': '0' * 64}):
            altered = deepcopy(later); altered[3].update(change)
            self.assertEqual(asof.replay(frozen, altered, decision)['results']['UP']['status'], 'rejected')

    def test_frozen_packet_and_declared_parameters_tamper_rejected(self):
        *_, frozen, later, decision = fixture()
        altered = deepcopy(frozen); altered['packet_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'integrity'):
            asof.replay(altered, later, decision)
        packet = json.loads(frozen['packet_json']); packet['parameters']['stabilization_wait_ms'] = 0
        altered['packet_json'] = base.encoded(packet); altered['packet_sha256'] = base.sha(altered['packet_json'])
        with self.assertRaisesRegex(ValueError, 'fixed parameters'):
            asof.replay(altered, later, decision)

    def test_directory_replay_matches_stored_outputs_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp); expected = directory_fixture(directory)
            with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')), \
                    patch('socket.socket.connect', side_effect=AssertionError('network')):
                self.assertEqual(asof.replay_directory(directory), expected)

    def test_directory_receipt_path_hash_and_source_membership_tamper_rejected(self):
        for attack in ('path', 'receipt_hash', 'bundle_hash', 'summary', 'hold'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp); directory_fixture(directory)
                if attack == 'receipt_hash':
                    path = directory / 'receipt-03.json'; path.write_text('{}', encoding='utf-8')
                else:
                    path = directory / ('started.json' if attack in ('bundle_hash', 'hold') else 'capture.json')
                    data = json.loads(path.read_bytes())
                    if attack == 'path': data['receipt_files'][0]['filename'] = '../receipt-01.json'
                    if attack == 'bundle_hash': data['source_bundle_sha256'] = '0' * 64
                    if attack == 'summary': data['summary']['live_ready'] = True
                    if attack == 'hold': data['hold_ms'] = 500
                    path.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(ValueError):
                    asof.replay_directory(directory)

    def test_invalid_capture_inputs_do_not_start_network(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(base, 'PublicReader') as reader:
            with self.assertRaises(ValueError):
                asof.capture(Path(tmp) / 'absent.json', Path(tmp) / 'new', Decimal('101'), 250)
            reader.assert_not_called()

    def test_capture_plan_and_source_bundle_exist_before_first_public_clock_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); selection = root / 'selection.json'; output = root / 'episode'
            selection.write_text('{"markets":[]}', encoding='utf-8')
            def inspect_plan(*args, **kwargs):
                plan = json.loads((output / 'started.json').read_bytes())
                self.assertEqual(plan['parameters'], asof.PARAMETERS)
                self.assertEqual(plan['shares'], '20')
                self.assertEqual(base.sha((output / 'source-bundle.json').read_bytes()), plan['source_bundle_sha256'])
                self.assertTrue((output / 'selection-frozen.json').exists())
                raise ValueError('Fixture clock unavailable')
            with patch.object(base, 'observe_public_clock', side_effect=inspect_plan):
                result = asof.capture(selection, output, Decimal('20'), 250)
            self.assertEqual(result['status'], 'capture_unavailable')
            self.assertEqual(json.loads((output / 'capture.json').read_bytes())['requests'], 0)

    def test_fixed_clock_wrapper_overhead_limit_is_enforced(self):
        _, clock, clocks, *_ = fixture()
        asof.verify_clock_binding(clock, clocks)
        changed = deepcopy(clocks)
        changed[0]['sent_local_ms'] += 101
        # Keep the outer clock observation untouched: forged inner provenance
        # cannot be used to claim that it bounded the original HTTP exchange.
        with self.assertRaises(ValueError):
            asof.verify_clock_binding(clock, changed)

    def test_research_result_never_credits_orders_prediction_or_profit(self):
        *_, frozen, later, decision = fixture()
        value = asof.replay(frozen, later, decision)
        self.assertEqual(value['model_calls'], 0); self.assertEqual(value['external_orders_submitted'], 0)
        for name in ('live_ready', 'prediction_credit', 'profitability_credit', 'actual_fee_verified',
                     'exchange_fills_verified', 'production_database_written', 'minimum_unit_conflict_resolved'):
            self.assertIs(value[name], False)


if __name__ == '__main__':
    unittest.main()
