"""Separate, predeclared public snapshot protocol assessed as of decision time.

V1 receipt-time captures are never reclassified. Fresh captures wait a fixed
second after ALL five inputs, retain original receipts, and require the entire
source-age interval to be [0, 5000] ms at the decision. Known-future-at-receipt
sources remain rejected. No model, wallet, order or production database access.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments import public_execution_episode as base
from experiments.public_execution_qualification import decision_age, minimum_scenarios

VERSION = 'public-fixed-wait-asof-decision-v2'
WAIT_MS = 1000
OVERSHOOT_MS = 250
SEAL_LAG_MS = 250
SOURCES = ['experiments/public_execution_asof.py',
           'experiments/public_execution_qualification.py',
           'experiments/public_execution_episode.py', 'polycast/public_clock.py',
           'polycast/market.py', 'polycast/net.py']
ERRORS = (ValueError, KeyError, TypeError, InvalidOperation)
PARAMETERS = {'stabilization_wait_ms': WAIT_MS, 'maximum_wait_overshoot_ms': OVERSHOOT_MS,
              'maximum_seal_lag_ms': SEAL_LAG_MS, 'maximum_source_age_ms': 5000,
              'maximum_input_receipt_age_ms': 5000, 'known_future_at_receipt_rejected': True,
              'clock_uncertainty_limit_ms': 750, 'maximum_public_gets': 12,
              'maximum_clock_wrapper_overhead_per_edge_ms': 100}


def point() -> dict:
    return {'local_ms': time.time_ns() // 1000000, 'monotonic_ns': time.monotonic_ns()}


def checked_point(value: dict) -> tuple[int, int]:
    if not isinstance(value, dict) or set(value) != {'local_ms', 'monotonic_ns'}:
        raise ValueError('Exact decision point required')
    if any(type(x) is not int or x <= 0 for x in value.values()):
        raise ValueError('Integer decision point required')
    return value['local_ms'], value['monotonic_ns']


def elapsed(before: dict, after: dict) -> float:
    local1, mono1 = checked_point(before); local2, mono2 = checked_point(after)
    delay = (mono2 - mono1) / 1000000
    if local2 < local1 or delay < 0 or abs(local2 - local1 - delay) > 100:
        raise ValueError('Decision ordering or wall-clock step')
    return delay


def receipt_point(receipt: dict, prefix: str = 'received') -> dict:
    return {'local_ms': receipt[prefix + '_local_ms'],
            'monotonic_ns': receipt[prefix + '_monotonic_ns']}


def sequence(receipts: list[dict], first: int) -> None:
    for index, receipt in enumerate(receipts, first):
        if type(receipt.get('request_number')) is not int or receipt['request_number'] != index:
            raise ValueError('Receipt request sequence mismatch')
        delay = elapsed(receipt_point(receipt, 'sent'), receipt_point(receipt))
        if delay > 5000:
            raise ValueError('Excessive transport latency')
        if index > first:
            elapsed(receipt_point(receipts[index - first - 1]), receipt_point(receipt, 'sent'))


def verify_clock_binding(clock: dict, receipts: list[dict]) -> None:
    base.verify_clock_receipts(clock, receipts)
    # The outer clock observer includes receipt serialization/fsync after the
    # inner HTTP receive. Preserve that wider interval, bound its overhead,
    # and never pretend the two instrumentation envelopes have equal times.
    for sample, receipt in zip(clock['samples'], receipts):
        before = receipt['sent_local_ms'] - sample['sent_local_ms']
        after = sample['received_local_ms'] - receipt['received_local_ms']
        overhead = sample['monotonic_elapsed_ms'] - (receipt['received_monotonic_ns'] -
                    receipt['sent_monotonic_ns']) / 1000000
        if not (-1 <= before <= 100 and -1 <= after <= 100 and -2 <= overhead <= 202):
            raise ValueError('Clock wrapper does not enclose bounded inner HTTP timing')


def decision_window(receipts: list[dict], decision: dict, clock: dict, expected: dict) -> dict:
    local, mono = checked_point(decision)
    if len(receipts) != 5:
        raise ValueError('Exactly five public inputs required')
    wait = elapsed(receipt_point(receipts[-1]), decision)
    if not WAIT_MS <= wait <= WAIT_MS + OVERSHOOT_MS:
        raise ValueError('Fixed stabilization wait not respected')
    for receipt in receipts:
        elapsed(receipt_point(receipt), decision)
        if local - receipt['received_local_ms'] > 5000:
            raise ValueError('Input receipt stale at shared decision')
    utc, uncertainty = base.clock_utc(clock, local)
    if utc - uncertainty < expected['start_at_ms'] or utc + uncertainty >= expected['end_at_ms']:
        raise ValueError('Shared decision outside verified market window')
    return {'local_ms': local, 'monotonic_ns': mono, 'wait_after_last_receipt_ms': wait,
            'utc_interval_ms': [str(utc - uncertainty), str(utc + uncertainty)]}


def age_proof(book: dict, receipt: dict, clock: dict, decision: dict) -> dict:
    local, mono = checked_point(decision)
    source = base.integer(book.get('timestamp'), 'book source timestamp')
    utc, width = base.clock_utc(clock, receipt['received_local_ms'])
    received_age = [utc - width - source, utc + width - source]
    if received_age[1] < 0:
        raise ValueError('Source known future at receipt; waiting cannot rescue it')
    proof = decision_age(book, receipt, clock, local, mono)
    if not proof['source_fresh'] or not proof['receipt_fresh']:
        raise ValueError('Full source/receipt age interval not fresh at decision')
    return {**proof, 'receipt_source_age_interval_ms': list(map(str, received_age)),
            'receipt_time_status': ('interval_ambiguous_at_receipt' if received_age[0] < 0
                                    else 'not_future_at_receipt'),
            'known_future_at_receipt': False, 'receipt_timestamps_modified': False}


def quote(book: dict, receipt: dict, clock: dict, contract: dict, asset: str,
          shares: Decimal, decision: dict, *, limit: Decimal | None = None) -> dict:
    """Validate unchanged receipt; compute depth separately from the V1 time rule."""
    proof = age_proof(book, receipt, clock, decision)
    if book.get('market') != contract['condition_id'] or book.get('asset_id') != asset:
        raise ValueError('Book identity mismatch')
    tick = Decimal(contract['tick']); minimum = Decimal(contract['minimum_from_clob'])
    if (base.number(book.get('tick_size'), 'book tick') != tick or
            base.number(book.get('min_order_size'), 'book minimum') != minimum):
        raise ValueError('Book constraints changed')
    if shares < minimum or shares != shares.quantize(Decimal('0.01')):
        raise ValueError('Below documented book-share minimum or wrong share precision')
    depth = {}
    for side in ('bids', 'asks'):
        rows = book.get(side)
        if not isinstance(rows, list):
            raise ValueError('Missing book depth')
        levels = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid depth row')
            price = base.number(row.get('price'), 'depth price')
            size = base.number(row.get('size'), 'depth size')
            if not 0 < price < 1 or size < 0 or price % tick != 0 or price in levels:
                raise ValueError('Invalid/off-grid/duplicate depth level')
            levels[price] = size
        depth[side] = {price: size for price, size in levels.items() if size > 0}
    if not depth['bids'] or not depth['asks'] or max(depth['bids']) >= min(depth['asks']):
        raise ValueError('One-sided or crossed book')
    remaining = shares; fills = []; gross = Decimal(0); fees = Decimal(0)
    for price, available in sorted(depth['asks'].items()):
        if limit is not None and price > limit:
            break
        size = min(remaining, available)
        fee = (size * Decimal(contract['fee_rate']) * price * (1 - price)).quantize(
            Decimal('0.00001'), rounding=ROUND_HALF_EVEN)
        fills.append({'price': str(price), 'shares': str(size), 'estimated_fee': str(fee)})
        gross += size * price; fees += fee; remaining -= size
        if remaining == 0:
            break
    if remaining != 0:
        raise ValueError('Full FOK depth unavailable within frozen limit')
    return {'asset_id': asset, 'shares': str(shares), 'limit_price': fills[-1]['price'],
            'average_price': str(gross / shares), 'gross_cost': str(gross),
            'estimated_fee': str(fees), 'estimated_total_cost': str(gross + fees), 'fills': fills,
            'fee_method': 'per-level-decimal-half-even-5dp-estimate-v1', 'age_proof': proof,
            'minimum_scenarios': minimum_scenarios({'shares': str(shares), 'gross_cost': str(gross)}, contract),
            'exchange_order_eligibility_verified': False}


def contract_from(expected: dict, receipts: list[dict]) -> dict:
    gamma = base.receipt_body(receipts[0], base.GAMMA + '/markets/' + expected['market_id'])
    event = base.receipt_body(receipts[1], base.GAMMA + '/events/' + expected['event_id'])
    clob = base.receipt_body(receipts[2], base.CLOB + '/clob-markets/' + gamma.get('conditionId', ''))
    return base.terms(expected, gamma, clob, event)


def initial_packet(expected: dict, clock: dict, receipts: list[dict], shares: Decimal,
                   hold_ms: int, decision: dict) -> dict:
    if not 0 < shares <= 100 or type(hold_ms) is not int or not 250 <= hold_ms <= 5000:
        raise ValueError('Bounded shares and hold time required')
    if len(receipts) != 5:
        raise ValueError('Exactly five initial inputs required')
    sequence(receipts, 3)
    timing = decision_window(receipts, decision, clock, expected)
    contract = contract_from(expected, receipts); quotes = {}
    for name, asset, receipt in zip(('UP', 'DOWN'), contract['assets'], receipts[3:]):
        try:
            body = base.receipt_body(receipt, base.CLOB + '/book?token_id=' + asset)
            value = quote(body, receipt, clock, contract, asset, shares, decision)
            quotes[name] = {'status': 'quoted', 'quote': value}
        except ERRORS as exc:
            quotes[name] = {'status': 'unavailable', 'reason': str(exc)}
    return {'version': VERSION, 'parameters': PARAMETERS, 'expected': expected, 'clock': clock,
            'shares': str(shares), 'requested_hold_ms': hold_ms, 'terms': contract,
            'initial_receipts': receipts, 'initial_decision': decision, 'initial_timing': timing,
            'initial_quotes': quotes, 'purpose': 'separate_public_asof_execution_diagnostic'}


def freeze(packet: dict, sealed: dict) -> dict:
    if elapsed(packet['initial_decision'], sealed) > SEAL_LAG_MS:
        raise ValueError('Initial packet seal lag exceeds fixed budget')
    local, mono = checked_point(sealed)
    utc, uncertainty = base.clock_utc(packet['clock'], local)
    if utc - uncertainty < packet['expected']['start_at_ms'] or utc + uncertainty >= packet['expected']['end_at_ms']:
        raise ValueError('Seal outside verified market window')
    for name, receipt in zip(('UP', 'DOWN'), packet['initial_receipts'][3:]):
        if packet['initial_quotes'][name]['status'] == 'quoted':
            age_proof(json.loads(receipt['raw_text']), receipt, packet['clock'], sealed)
    raw = base.encoded(packet)
    return {'schema_version': 1, 'sealed_local_ms': local, 'sealed_monotonic_ns': mono,
            'packet_json': raw, 'packet_sha256': base.sha(raw)}


def replay(frozen: dict, later: list[dict], decision: dict) -> dict:
    raw = frozen.get('packet_json')
    if (type(frozen.get('schema_version')) is not int or frozen['schema_version'] != 1 or
            not isinstance(raw, str) or base.sha(raw) != frozen.get('packet_sha256')):
        raise ValueError('Sealed intent integrity mismatch')
    packet = json.loads(raw)
    if packet.get('version') != VERSION or packet.get('parameters') != PARAMETERS:
        raise ValueError('Separate as-of protocol and fixed parameters required')
    rebuilt = initial_packet(packet['expected'], packet['clock'], packet['initial_receipts'],
                             Decimal(packet['shares']), packet['requested_hold_ms'], packet['initial_decision'])
    sealed = {'local_ms': frozen['sealed_local_ms'], 'monotonic_ns': frozen['sealed_monotonic_ns']}
    if rebuilt != packet or freeze(rebuilt, sealed) != frozen:
        raise ValueError('Frozen intent differs from original inputs')
    if len(later) != 5:
        raise ValueError('Exactly five later public inputs required')
    sequence(later, 8)
    hold = max(packet['requested_hold_ms'], packet['terms']['snapshot_hold_ms'])
    for receipt in later:
        if elapsed(sealed, receipt_point(receipt, 'sent')) < hold:
            raise ValueError('Later request preceded required sealed hold')
    rejection = None; timing = None
    try:
        timing = decision_window(later, decision, packet['clock'], packet['expected'])
        if contract_from(packet['expected'], later) != packet['terms']:
            raise ValueError('Market terms changed after seal')
    except ERRORS as exc:
        rejection = str(exc)
    results = {}
    for name, asset, receipt in zip(('UP', 'DOWN'), packet['terms']['assets'], later[3:]):
        try:
            initial = packet['initial_quotes'][name]
            if initial['status'] != 'quoted':
                raise ValueError('Initial quote unavailable: ' + initial['reason'])
            if rejection:
                raise ValueError(rejection)
            original_quote = initial['quote']
            body = base.receipt_body(receipt, base.CLOB + '/book?token_id=' + asset)
            value = quote(body, receipt, packet['clock'], packet['terms'], asset,
                          Decimal(packet['shares']), decision, limit=Decimal(original_quote['limit_price']))
            if Decimal(value['estimated_total_cost']) > Decimal(original_quote['estimated_total_cost']):
                raise ValueError('Estimated cost exceeds frozen reserve')
            both = (original_quote['minimum_scenarios']['both_scenarios_met'] and
                    value['minimum_scenarios']['both_scenarios_met'])
            results[name] = {'status': 'asof_snapshot_qualified' if both else 'minimum_scenario_rejected',
                'quote': value, 'frozen_limit_price': original_quote['limit_price'],
                'frozen_estimated_reserve': original_quote['estimated_total_cost'],
                'initial_minimum_scenarios': original_quote['minimum_scenarios'],
                'price_slippage_per_share': str(Decimal(value['average_price']) - Decimal(original_quote['average_price']))}
        except ERRORS as exc:
            results[name] = {'status': 'rejected', 'reason': str(exc)}
    return {'schema_version': 1, 'version': VERSION, 'market_id': packet['expected']['market_id'],
            'sealed_packet_sha256': frozen['packet_sha256'], 'parameters': PARAMETERS,
            'initial_timing': packet['initial_timing'], 'later_timing': timing,
            'minimum_hold_ms': hold, 'results': results,
            'old_receipt_time_captures_reclassified': False, 'minimum_unit_conflict_resolved': False,
            'actual_fee_verified': False, 'exchange_fills_verified': False,
            'exchange_order_eligibility_verified': False, 'prediction_credit': False,
            'profitability_credit': False, 'live_ready': False, 'model_calls': 0,
            'external_orders_submitted': 0, 'production_database_written': False,
            'limitations': base.LIMITATIONS + [
                'New fixed-wait cohort; cannot rescue or pool rejected V1 receipt-time episodes',
                'Waiting changes the information and latency policy; no claimed speed or alpha benefit',
                'Book persistence during wait and transport is not proven; no observed exchange fill',
                'UP and DOWN are alternative diagnostics, not simultaneous orders']}


def fixed_wait(receipts: list[dict]) -> dict:
    target = receipts[-1]['received_monotonic_ns'] + WAIT_MS * 1000000
    while time.monotonic_ns() < target:
        time.sleep(max(0, (target - time.monotonic_ns()) / 1000000000))
    return point()


def capture(selection_path: Path, output: Path, shares: Decimal, hold_ms: int) -> dict:
    if not 0 < shares <= 100 or type(hold_ms) is not int or not 250 <= hold_ms <= 5000:
        raise ValueError('Bounded shares and hold time required')
    output.mkdir(parents=True, exist_ok=False)
    selection_raw = selection_path.read_bytes(); selection = json.loads(selection_raw)
    bundle = {name: {'sha256': base.sha((ROOT / name).read_bytes()),
                     'text': (ROOT / name).read_bytes().decode('utf-8')} for name in SOURCES}
    base.write_new(output / 'selection-frozen.json', selection)
    base.write_new(output / 'source-bundle.json', bundle)
    base.write_new(output / 'started.json', {'version': VERSION, 'started': point(),
        'parameters': PARAMETERS, 'shares': str(shares), 'hold_ms': hold_ms,
        'selection_original_sha256': base.sha(selection_raw),
        'selection_frozen_sha256': base.sha((output / 'selection-frozen.json').read_bytes()),
        'source_bundle_sha256': base.sha((output / 'source-bundle.json').read_bytes()),
        'source_hashes': {name: item['sha256'] for name, item in bundle.items()},
        'purpose': 'Fixed public as-of diagnostic plan before requests; no prospective forecast credit'})
    reader = base.PublicReader(output); result = None; later = []
    status = 'capture_unavailable'
    try:
        clock = base.observe_public_clock(fetch=reader.clock_fetch)
        verify_clock_binding(clock, reader.receipts[:2])
        utc, uncertainty = base.clock_utc(clock, point()['local_ms'])
        candidates = [m for m in selection['markets'] if
            m['start_at_ms'] <= utc - uncertainty and utc + uncertainty + 15000 < m['end_at_ms']]
        if len(candidates) != 1:
            raise ValueError('Exactly one current market with capture time remaining required')
        expected = candidates[0]
        gamma_r = reader.get(base.GAMMA + '/markets/' + expected['market_id'])
        gamma = base.receipt_body(gamma_r, gamma_r['url'])
        event_r = reader.get(base.GAMMA + '/events/' + expected['event_id'])
        condition = gamma.get('conditionId', '')
        clob_r = reader.get(base.CLOB + '/clob-markets/' + condition)
        base.terms(expected, gamma, base.receipt_body(clob_r, clob_r['url']),
                   base.receipt_body(event_r, event_r['url']))
        initial = [gamma_r, event_r, clob_r] + [reader.get(base.CLOB + '/book?token_id=' + asset)
            for asset in (expected['up_asset'], expected['down_asset'])]
        decision = fixed_wait(initial)
        packet = initial_packet(expected, clock, initial, shares, hold_ms, decision)
        frozen = freeze(packet, point())
        base.write_new(output / 'frozen-intent.json', frozen)
        hold = max(hold_ms, packet['terms']['snapshot_hold_ms'])
        target = frozen['sealed_monotonic_ns'] + hold * 1000000
        while time.monotonic_ns() < target:
            time.sleep(max(0, (target - time.monotonic_ns()) / 1000000000))
        later = [reader.get(base.GAMMA + '/markets/' + expected['market_id']),
                 reader.get(base.GAMMA + '/events/' + expected['event_id']),
                 reader.get(base.CLOB + '/clob-markets/' + condition)] + [
                 reader.get(base.CLOB + '/book?token_id=' + asset)
                 for asset in (expected['up_asset'], expected['down_asset'])]
        later_decision = fixed_wait(later)
        base.write_new(output / 'later-decision.json', later_decision)
        result = replay(frozen, later, later_decision)
        base.write_new(output / 'replay.json', result)
        status = 'capture_completed'
        return result
    except ERRORS + (OSError,) as exc:
        result = {'version': VERSION, 'status': status, 'reason': str(exc)[:250],
                  'model_calls': 0, 'external_orders_submitted': 0, 'live_ready': False,
                  'production_database_written': False}
        return result
    finally:
        base.write_new(output / 'capture.json', {'version': VERSION, 'status': status,
            'requests': len(reader.receipts), 'maximum_requests': base.MAX_REQUESTS,
            'receipt_files': [{'filename': f'receipt-{i:02}.json', 'sha256': base.sha(
                (output / f'receipt-{i:02}.json').read_bytes())} for i in range(1, len(reader.receipts) + 1)],
            'later_request_numbers': [r['request_number'] for r in later], 'summary': result})


def replay_directory(directory: Path) -> dict:
    manifest = json.loads((directory / 'capture.json').read_bytes())
    if (manifest.get('version') != VERSION or manifest.get('status') != 'capture_completed' or
            manifest.get('requests') != 12 or manifest.get('maximum_requests') != 12 or
            len(manifest['receipt_files']) != 12 or manifest['later_request_numbers'] != list(range(8, 13))):
        raise ValueError('Complete twelve-request matching capture required')
    started = json.loads((directory / 'started.json').read_bytes())
    if started['version'] != VERSION or started['parameters'] != PARAMETERS:
        raise ValueError('Capture plan protocol changed')
    for filename, key in [('selection-frozen.json', 'selection_frozen_sha256'),
                          ('source-bundle.json', 'source_bundle_sha256')]:
        if base.sha((directory / filename).read_bytes()) != started[key]:
            raise ValueError('Capture plan file changed')
    bundle = json.loads((directory / 'source-bundle.json').read_bytes())
    if set(bundle) != set(SOURCES) or set(started['source_hashes']) != set(SOURCES):
        raise ValueError('Source bundle membership mismatch')
    for name, item in bundle.items():
        if (base.sha(item['text']) != item['sha256'] or item['sha256'] != started['source_hashes'][name] or
                base.sha((ROOT / name).read_bytes()) != item['sha256']):
            raise ValueError('Frozen/current replay source differs')
    receipts = []
    for index, entry in enumerate(manifest['receipt_files'], 1):
        if entry['filename'] != f'receipt-{index:02}.json':
            raise ValueError('Receipt sequence/path mismatch')
        raw = (directory / entry['filename']).read_bytes()
        if base.sha(raw) != entry['sha256']:
            raise ValueError('Receipt artifact hash mismatch')
        receipts.append(json.loads(raw))
    sequence(receipts, 1)
    elapsed(started['started'], receipt_point(receipts[0], 'sent'))
    frozen = json.loads((directory / 'frozen-intent.json').read_bytes())
    packet = json.loads(frozen['packet_json'])
    if (packet['initial_receipts'] != receipts[2:7] or packet['shares'] != started['shares'] or
            packet['requested_hold_ms'] != started['hold_ms']):
        raise ValueError('Frozen input/parameter provenance mismatch')
    verify_clock_binding(packet['clock'], receipts[:2])
    if not receipts[1]['received_local_ms'] - 1 <= packet['clock']['checked_at_local_ms'] <= receipts[2]['sent_local_ms'] + 1:
        raise ValueError('Clock verification not between its receipts and initial inputs')
    selection = json.loads((directory / 'selection-frozen.json').read_bytes())
    if selection['markets'].count(packet['expected']) != 1:
        raise ValueError('Selected market not bound to frozen discovery')
    decision = json.loads((directory / 'later-decision.json').read_bytes())
    result = replay(frozen, receipts[7:], decision)
    if result != manifest['summary'] or result != json.loads((directory / 'replay.json').read_bytes()):
        raise ValueError('Stored replay differs from captured evidence')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('capture'); c.add_argument('--selection', type=Path, required=True)
    c.add_argument('--output', type=Path, required=True); c.add_argument('--shares', default='20')
    c.add_argument('--hold-ms', type=int, default=250)
    r = sub.add_parser('replay'); r.add_argument('--directory', type=Path, required=True)
    r.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'capture':
        result = capture(args.selection, args.output, base.number(args.shares, 'shares'), args.hold_ms)
    else:
        if args.output.exists():
            parser.error('New replay output required')
        result = replay_directory(args.directory); base.write_new(args.output, result)
    print(json.dumps({key: value for key, value in result.items()
                     if key in ('version', 'status', 'reason', 'market_id', 'results', 'live_ready')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
