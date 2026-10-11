"""Qualify public snapshot diagnostics at a shared decision time, never submit orders.

Keeps the original V1 capture and replay unchanged. Adds initial-seal/later-pair
freshness checks and explicit share/notional minimum scenarios. No credentials,
production database, model calls, wallet access or exchange acceptance claim.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments import public_execution_episode as original

VERSION = 'public-shared-decision-qualification-v1'
SOURCES = ['experiments/public_execution_qualification.py',
           'experiments/public_execution_episode.py', 'polycast/public_clock.py',
           'polycast/market.py', 'polycast/net.py']


def decision_age(book: dict, receipt: dict, clock: dict,
                 local_ms: int, monotonic_ns: int) -> dict:
    """Prove age at a decision after receipt; never repair the source timestamp."""
    original.integer(local_ms, 'decision local time')
    original.integer(monotonic_ns, 'decision monotonic time')
    received = original.integer(receipt.get('received_local_ms'), 'receipt local time')
    received_mono = original.integer(receipt.get('received_monotonic_ns'), 'receipt monotonic time')
    if local_ms < received or monotonic_ns < received_mono:
        raise ValueError('Decision preceded receipt')
    elapsed = (monotonic_ns - received_mono) / 1000000
    if abs((local_ms - received) - elapsed) > 100:
        raise ValueError('Wall-clock step between receipt and decision')
    utc, uncertainty = original.clock_utc(clock, local_ms)
    source = original.integer(book.get('timestamp'), 'book source timestamp')
    lower, upper = utc - uncertainty - source, utc + uncertainty - source
    receipt_age = local_ms - received
    return {'decision_local_ms': local_ms, 'decision_monotonic_ns': monotonic_ns,
            'source_timestamp_ms': source, 'source_age_interval_ms': [str(lower), str(upper)],
            'receipt_age_ms': receipt_age, 'elapsed_since_receipt_ms': elapsed,
            'source_fresh': lower >= 0 and upper <= 5000,
            'receipt_fresh': receipt_age <= 5000,
            'age_limit_ms': 5000}


def minimum_scenarios(quote: dict, terms: dict) -> dict:
    shares = original.number(quote.get('shares'), 'quote shares')
    gross = original.number(quote.get('gross_cost'), 'quote gross cost')
    minimum_shares = original.number(terms.get('minimum_from_clob'), 'book share minimum')
    minimum_cash = original.number(terms.get('minimum_from_gamma'), 'Gamma notional scenario')
    share_met, cash_met = shares >= minimum_shares, gross >= minimum_cash
    return {'shares': str(shares), 'gross_collateral_excluding_fees': str(gross),
            'documented_book_shares': {'minimum': str(minimum_shares), 'met': share_met},
            'documented_gamma_notional': {'minimum': str(minimum_cash), 'met': cash_met},
            'both_scenarios_met': share_met and cash_met,
            'unit_conflict_resolved': False,
            'limitation': 'Notional check is a conservative scenario on sampled gross cost; actual exchange minimum semantics are unverified'}


def qualify(frozen: dict, later: list[dict]) -> dict:
    base = original.replay(frozen, later)
    packet = json.loads(frozen['packet_json'])
    clock, terms = packet['clock'], packet['terms']
    # Both alternatives are assessed only after all later metadata and both
    # books arrived. They are alternative diagnostics, never simultaneous trades.
    common_local = max(item['received_local_ms'] for item in later)
    common_mono = max(item['received_monotonic_ns'] for item in later)
    decision_utc, uncertainty = original.clock_utc(clock, common_local)
    decision_in_window = (packet['expected']['start_at_ms'] <= decision_utc - uncertainty
                          and decision_utc + uncertainty < packet['expected']['end_at_ms'])
    results = {}
    for name, asset, before, after in zip(('UP', 'DOWN'), terms['assets'],
                                        packet['initial_receipts'][3:], later[3:]):
        result = {'original_snapshot_status': base['results'][name]['status'],
                  'qualified': False, 'reasons': [], 'exchange_order_eligibility_verified': False}
        if not decision_in_window:
            result['reasons'].append('Shared later decision outside verified market window')
        try:
            for stage, receipt, local_ms, mono_ns in [
                    ('initial_seal', before, frozen['sealed_local_ms'], frozen['sealed_monotonic_ns']),
                    ('later_shared_decision', after, common_local, common_mono)]:
                body = original.receipt_body(receipt, original.CLOB + '/book?token_id=' + asset)
                ages = decision_age(body, receipt, clock, local_ms, mono_ns)
                result[stage + '_age'] = ages
                if not ages['source_fresh'] or not ages['receipt_fresh']:
                    result['reasons'].append(stage + ': book is not proven fresh at decision')
            initial = packet['initial_quotes'][name]
            if initial['status'] == 'quoted':
                scenario = minimum_scenarios(initial['quote'], terms)
                result['initial_minimum_scenarios'] = scenario
                if not scenario['both_scenarios_met']:
                    result['reasons'].append('Initial quote fails at least one documented minimum scenario')
            else:
                result['reasons'].append('Original initial quote unavailable: ' + initial['reason'])
            outcome = base['results'][name]
            if outcome['status'] == 'snapshot_fillable':
                scenario = minimum_scenarios(outcome['quote'], terms)
                result['later_minimum_scenarios'] = scenario
                if not scenario['both_scenarios_met']:
                    result['reasons'].append('Later quote fails at least one documented minimum scenario')
            else:
                result['reasons'].append('Original delayed replay rejected: ' + outcome['reason'])
            result['qualified'] = not result['reasons']
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            result['reasons'].append(str(exc))
        results[name] = result
    return {'version': VERSION, 'market_id': packet['expected']['market_id'],
            'sealed_packet_sha256': frozen['packet_sha256'], 'shared_later_decision_local_ms': common_local,
            'shared_later_decision_monotonic_ns': common_mono,
            'shared_later_decision_in_market_window': decision_in_window, 'results': results,
            'both_alternative_snapshots_qualified': all(item['qualified'] for item in results.values()),
            'original_receipt_freshness_rules_unchanged': True,
            'original_outputs_modified': False, 'minimum_unit_conflict_resolved': False,
            'actual_fee_verified': False, 'exchange_fills_verified': False,
            'exchange_order_eligibility_verified': False, 'prediction_credit': False,
            'profitability_credit': False, 'live_ready': False, 'model_calls': 0,
            'external_orders_submitted': 0, 'production_database_written': False,
            'limitations': ['Checks aggregated snapshots, not exchange acceptance or actual fills',
                            'Unchanged receipt-time checks plus stricter common-decision age checks',
                            'Both quantity scenarios passing does not resolve the documentation conflict',
                            'Fees remain per-level estimates; match splitting and exact rounding are unknown',
                            'No model probabilities, official outcomes or complete research costs']}


def replay_directory(directory: Path) -> dict:
    original.replay_directory(directory)
    frozen = json.loads((directory / 'frozen-intent.json').read_text(encoding='utf-8'))
    later = [json.loads((directory / f'receipt-{i:02}.json').read_text(encoding='utf-8'))
             for i in range(8, 13)]
    return qualify(frozen, later)


def source_bundle() -> dict:
    result = {}
    for name in SOURCES:
        raw = (ROOT / name).read_bytes()
        result[name] = {'sha256': original.sha(raw), 'text': raw.decode('utf-8')}
    return result


def capture(selection: Path, output: Path, shares: Decimal, hold_ms: int) -> dict:
    if not 0 < shares <= 100 or type(hold_ms) is not int or not 250 <= hold_ms <= 5000:
        raise ValueError('Bounded shares and hold time required')
    output.mkdir(parents=True, exist_ok=False)
    selection_raw = selection.read_bytes()
    parsed_selection = json.loads(selection_raw)
    bundle = source_bundle()
    original.write_new(output / 'source-bundle.json', bundle)
    original.write_new(output / 'selection-frozen.json', parsed_selection)
    original.write_new(output / 'started.json', {'version': VERSION,
        'started_local_ms': int(time.time_ns() / 1000000), 'shares': str(shares), 'hold_ms': hold_ms,
        'maximum_public_gets': original.MAX_REQUESTS, 'selection_original_sha256': original.sha(selection_raw),
        'selection_frozen_sha256': original.sha((output / 'selection-frozen.json').read_bytes()),
        'source_bundle_sha256': original.sha((output / 'source-bundle.json').read_bytes()),
        'source_hashes': {name: item['sha256'] for name, item in bundle.items()},
        'purpose': 'Public decision-time execution qualification; not prospective strategy registration',
        'model_calls': 0, 'external_orders_submitted': 0, 'live_ready': False})
    base = original.capture(output / 'selection-frozen.json', output / 'capture', shares, hold_ms)
    manifest = json.loads((output / 'capture/capture.json').read_text(encoding='utf-8'))
    if manifest['status'] == 'capture_completed':
        result = replay_directory(output / 'capture')
    else:
        result = {'version': VERSION, 'status': 'capture_unavailable', 'reason': base.get('reason'),
                  'model_calls': 0, 'external_orders_submitted': 0, 'live_ready': False}
    original.write_new(output / 'qualification.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('capture')
    c.add_argument('--selection', type=Path, required=True)
    c.add_argument('--output', type=Path, required=True)
    c.add_argument('--shares', default='20')
    c.add_argument('--hold-ms', type=int, default=250)
    r = sub.add_parser('replay')
    r.add_argument('--directory', type=Path, required=True)
    r.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'capture':
        result = capture(args.selection, args.output, original.number(args.shares, 'shares'), args.hold_ms)
    else:
        if args.output.exists():
            parser.error('New output file required')
        result = replay_directory(args.directory)
        original.write_new(args.output, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
