"""Empirical BTC outcome probabilities from frozen relay/spot features.

Read-only research. Market probabilities and AI probabilities are benchmarks,
never fitting features. Chronological training uses verified outcomes actually
known before the target, with a 60-minute embargo. No production DB writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
import warnings
from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.btc5m_model_diagnostics import build_report, frozen_proxy, probability
from experiments.calibration_live_shadow import (PACKAGES, TARGET_SQL, connect_readonly,
    fingerprint, source_hashes, target_record, write_once, write_report)
from experiments.calibration_walkforward import logit
from polycast.market import parse_utc

EXPERIMENT = 'btc5m-empirical-price-probability-v1'
MODEL = 'deepseek-v4-pro'
PROMPT = 'btc5m-v6-explicit-utc-volatility-units'
PARAMETERS = {'C': 1.0, 'solver': 'lbfgs', 'max_iter': 1000, 'random_state': 0,
    'epsilon': 1e-6, 'min_train_events': 20, 'min_each_outcome': 5,
    'embargo_minutes': 60, 'maximum_events': 3, 'maximum_watch_seconds': 1100,
    'max_input_age_seconds': 30, 'minimum_remaining_seconds': 60, 'poll_seconds': 3}
FEATURES = ['logit(relay_diffusion_proxy)', 'logit(spot_diffusion_proxy)']


def utcnow():
    return datetime.now(timezone.utc)


def registered_sources():
    return {**source_hashes(ROOT), str(Path(__file__).relative_to(ROOT)).replace('\\', '/'):
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def features(item):
    if not all(probability(item.get(name)) for name in ('relay', 'spot')):
        raise ValueError('Both frozen price proxies are required')
    return [logit(item[name], PARAMETERS['epsilon']) for name in ('relay', 'spot')]


def available_input(conn, payload):
    """Do not learn from a forecast that contained any future/late evidence."""
    as_of = parse_utc(payload.get('as_of'))
    if not as_of or not isinstance(payload.get('evidence'), list) or not payload['evidence']:
        return False
    for item in payload['evidence']:
        if not isinstance(item, dict):
            return False
        row = conn.execute('SELECT fetched_at FROM evidence WHERE id=?', (item.get('id'),)).fetchone()
        times = [parse_utc(item.get(name)) for name in ('published_at', 'first_seen_at')]
        times.append(parse_utc(row['fetched_at']) if row else None)
        if not all(times) or max(times) > as_of:
            return False
    return True


def causal_history(conn, now):
    original = build_report(conn, now)['events']
    eligible, rejected = [], Counter()
    for event in original:
        if (event['model_version'], event['prompt_version']) != (MODEL, PROMPT):
            continue
        if event['analysis_abstain']:
            rejected['original_abstention'] += 1
            continue
        if not all(probability(event.get(key)) for key in ('relay', 'spot')):
            rejected['missing_price_proxy'] += 1
            continue
        rows = conn.execute('''SELECT p.as_of,d.payload_json,d.created_at
            FROM predictions p JOIN analysis_drafts d ON d.market_id=p.market_id
              AND d.as_of=p.as_of AND d.rules_hash=p.rules_hash AND d.evidence_hash=p.evidence_hash
            WHERE p.id=?''', (event['prediction_id'],)).fetchall()
        if len(rows) != 1:
            rejected['ambiguous_original_input'] += 1
            continue
        payload = json.loads(rows[0]['payload_json'])
        at = parse_utc(rows[0]['as_of'])
        if (not available_input(conn, payload) or not at <= parse_utc(rows[0]['created_at'])
                <= parse_utc(event['reviewed_at'])):
            rejected['causal_time_failure'] += 1
            continue
        eligible.append({**event, 'as_of': rows[0]['as_of']})
    return sorted(eligible, key=lambda e: (parse_utc(e['as_of']), e['prediction_id'])), dict(rejected)


def training_for(history, target):
    as_of = parse_utc(target['as_of'])
    return [e for e in history if e['event_id'] != target['event_id']
            and e['model_version'] == MODEL and e['prompt_version'] == PROMPT
            and parse_utc(e['end_at']) <= as_of - timedelta(minutes=PARAMETERS['embargo_minutes'])
            and parse_utc(e['as_of']) < as_of
            and parse_utc(e['resolved_at']) < as_of and parse_utc(e['recorded_at']) < as_of]


def fit_prices(training):
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import LogisticRegression
    if len({e['event_id'] for e in training}) != len(training):
        raise ValueError('Duplicate training event')
    model = LogisticRegression(**{key: PARAMETERS[key] for key in
        ('C', 'solver', 'max_iter', 'random_state')})
    with warnings.catch_warnings():
        warnings.simplefilter('error', ConvergenceWarning)
        model.fit([features(e) for e in training], [e['outcome'] for e in training])
    weights, intercept = [float(v) for v in model.coef_[0]], float(model.intercept_[0])
    if not all(math.isfinite(v) for v in [*weights, intercept]) or sum(weights) <= 0:
        raise ValueError('Nonfinite fit or reversed joint price direction')
    return {'coefficients': weights, 'intercept': intercept}


def predict_prices(item, fitted):
    weights, intercept = fitted.get('coefficients'), fitted.get('intercept')
    if (not isinstance(weights, list) or len(weights) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) for v in [*weights, intercept])
            or sum(weights) <= 0):
        raise ValueError('Invalid fit')
    z = fitted['intercept'] + sum(a*b for a, b in zip(features(item), fitted['coefficients']))
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    exp = math.exp(z)
    return exp / (1 + exp)


def candidate(target, history, fit=fit_prices):
    training = training_for(history, target)
    record = {**target, 'learned': None, 'fitted': None, 'training_events': len(training),
        'training_event_ids': [e['event_id'] for e in training], 'training_sha256': fingerprint(training)}
    counts = Counter(e['outcome'] for e in training)
    if len(training) < PARAMETERS['min_train_events']:
        record['status'] = 'insufficient_training'
    elif min(counts.get(0, 0), counts.get(1, 0)) < PARAMETERS['min_each_outcome']:
        record['status'] = 'insufficient_outcome_coverage'
    else:
        try:
            fitted = fit(training)
            predicted = predict_prices(target, fitted)
            if not probability(predicted):
                raise ValueError('Invalid fitted probability')
            record.update(status='candidate', learned=predicted, fitted=fitted)
        except Exception as exc:
            record.update(status='fit_failed', failure_type=type(exc).__name__)
    return record


def metrics(items):
    fields = ('model', 'relay', 'spot', 'market', 'learned')
    paired = [e for e in items if e['status'] == 'candidate'
              and e.get('outcome') in (0, 1) and all(probability(e.get(k)) for k in fields)]
    return {'same_events': len(paired), 'brier': {key: round(sum(
        (e[key]-e['outcome'])**2 for e in paired)/len(paired), 6) if paired else None for key in fields},
        'directional_correct': {key: sum((e[key] > .5) == bool(e['outcome']) for e in paired
                                         if e[key] != .5) for key in fields},
        'directional_events': {key: sum(e[key] != .5 for e in paired) for key in fields}}


def historical(database, output):
    conn = connect_readonly(database)
    try:
        conn.execute('BEGIN')
        history, excluded = causal_history(conn, utcnow())
    finally:
        conn.close()
    records = [candidate(target, history) for target in history]
    result = {'experiment': EXPERIMENT, 'checked_at': utcnow().isoformat(),
        'parameters': PARAMETERS, 'features': FEATURES, 'source_hashes': registered_sources(),
        'packages': {name: version(name) for name in PACKAGES}, 'excluded': excluded,
        'selected': len(history), 'status_counts': dict(Counter(e['status'] for e in records)),
        'metrics': metrics(records), 'records': records,
        'scope': 'Exploratory chronology only; method designed after prior results; no prospective credit'}
    write_report(output / 'historical.json', result)
    print(json.dumps({key: result[key] for key in ('selected', 'excluded', 'status_counts', 'metrics')}), flush=True)


def publication_valid(record, receipt, manifest, now=None):
    item = dict(record)
    digest = item.pop('record_sha256', None)
    at, end, reviewed = map(parse_utc, (item.get('as_of'), item.get('end_at'), item.get('reviewed_at')))
    written, published = map(parse_utc, (item.get('recorded_at'), receipt.get('published_at')))
    return (digest == fingerprint(item) and item.get('manifest_sha256') == fingerprint(manifest)
        and receipt.get('record_sha256') == digest
        and receipt.get('manifest_sha256') == fingerprint(manifest)
        and all((at, end, reviewed, written, published))
        and parse_utc(manifest['registered_at']) <= at <= reviewed <= written <= published < end
        and (published-at).total_seconds() <= PARAMETERS['max_input_age_seconds']
        and published <= (now or utcnow()))


def evaluate(output, database):
    manifest = json.loads((output/'manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('experiment') != EXPERIMENT or manifest.get('parameters') != PARAMETERS
            or manifest.get('features') != FEATURES or manifest.get('packages') != PACKAGES
            or manifest.get('model_version') != MODEL or manifest.get('prompt_version') != PROMPT):
        raise ValueError('Registration method mismatch')
    for name, digest in manifest['source_hashes'].items():
        path = output/'registered-sources'/name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Registered source copy mismatch')
    now = utcnow()
    scored, pending, invalid = [], 0, 0
    conn = connect_readonly(database)
    try:
        conn.execute('BEGIN')
        for path in sorted((output/'events').glob('*.json')):
            try:
                item = json.loads(path.read_text(encoding='utf-8'))
                receipt = json.loads((output/'receipts'/path.name).read_text(encoding='utf-8'))
                if (path.stem != hashlib.sha256(item['event_id'].encode()).hexdigest()
                        or not publication_valid(item, receipt, manifest, now)):
                    raise ValueError('Publication mismatch')
                parent = conn.execute('''SELECT d.payload_json,d.result_json,m.event_id,
                    p.model_output_json FROM predictions p JOIN markets m ON m.id=p.market_id
                    JOIN analysis_drafts d ON d.market_id=p.market_id AND d.as_of=p.as_of
                      AND d.rules_hash=p.rules_hash AND d.evidence_hash=p.evidence_hash
                    WHERE p.id=? AND p.market_id=?''', (item['prediction_id'], item['market_id'])).fetchall()
                if len(parent) != 1:
                    raise ValueError('Ambiguous parent')
                payload, original = json.loads(parent[0]['payload_json']), json.loads(parent[0]['model_output_json'])
                if (parent[0]['event_id'] != item['event_id'] or payload['as_of'] != item['as_of']
                        or hashlib.sha256(parent[0]['payload_json'].encode()).hexdigest() != item['payload_sha256']
                        or hashlib.sha256(parent[0]['result_json'].encode()).hexdigest() != item['draft_sha256']
                        or original.get('model_version') != MODEL or original.get('prompt_version') != PROMPT
                        or original.get('p_yes') != item['model']
                        or not available_input(conn, payload)
                        or any(frozen_proxy(payload, name, parse_utc(item['as_of'])) != item[name]
                               for name in ('relay', 'spot'))):
                    raise ValueError('Original input mismatch')
                if item['status'] == 'candidate' and predict_prices(item, item['fitted']) != item['learned']:
                    raise ValueError('Candidate does not match registered fit')
                training = item['training_data']
                if (fingerprint(training) != item['training_sha256'] or
                        [e['event_id'] for e in training] != item['training_event_ids'] or
                        len(training) != item['training_events'] or
                        training_for(training, item) != training):
                    raise ValueError('Training provenance or chronology mismatch')
                if item['status'] == 'candidate':
                    replay = fit_prices(training)
                    if not math.isclose(predict_prices(item, replay), item['learned'], abs_tol=1e-10, rel_tol=1e-10):
                        raise ValueError('Probability cannot be reproduced from saved training')
                result = conn.execute('SELECT * FROM resolutions WHERE market_id=?', (item['market_id'],)).fetchone()
                if not result or parse_utc(result['recorded_at']) > now:
                    pending += 1
                    continue
                if (not parse_utc(receipt['published_at']) < parse_utc(result['resolved_at'])
                        or not parse_utc(item['end_at']) <= parse_utc(result['resolved_at'])
                        or result['winning_side'] not in ('YES', 'NO')
                        or not parse_utc(result['resolved_at']) <= parse_utc(result['recorded_at']) <= now):
                    raise ValueError('Invalid result chronology')
                scored.append({**item, 'outcome': int(result['winning_side'] == 'YES')})
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                invalid += 1
    finally:
        conn.close()
    result = {'experiment': EXPERIMENT, 'checked_at': now.isoformat(), 'invalid': invalid,
        'pending': pending, 'scored': len(scored), 'metrics': metrics(scored), 'events': scored,
        'orders_submitted': 0, 'scope': 'Actual pre-outcome publications only; small dependent samples, no admission credit'}
    write_report(output/'evaluation.json', result)
    return result


def watch(database, output, seconds):
    if not 1 <= seconds <= PARAMETERS['maximum_watch_seconds']:
        raise ValueError('Observation limit exceeded')
    if {name: version(name) for name in PACKAGES} != PACKAGES:
        raise ValueError('Pinned dependency mismatch')
    manifest = {'experiment': EXPERIMENT, 'registered_at': utcnow().isoformat(),
        'model_version': MODEL, 'prompt_version': PROMPT, 'features': FEATURES,
        'parameters': PARAMETERS, 'packages': PACKAGES, 'source_hashes': registered_sources(),
        'watch_seconds': seconds, 'scope': 'Read-only future probability research; no orders',
        'selection': 'First eligible non-abstaining original parent per future event; retain both review decisions'}
    if not write_once(output/'manifest.json', manifest):
        raise ValueError('Existing registration cannot be repeated')
    for name, digest in manifest['source_hashes'].items():
        target = output/'registered-sources'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != digest:
            raise ValueError('Source changed during registration')
        target.write_bytes((ROOT/name).read_bytes())
    print(json.dumps({'registered_at': manifest['registered_at']}), flush=True)
    until, saved = time.monotonic()+seconds, 0
    status = 'bounded_observation_completed'
    while time.monotonic() < until and saved < PARAMETERS['maximum_events']:
        if registered_sources() != manifest['source_hashes']:
            status = 'source_freeze_changed'
            break
        now = utcnow()
        conn = connect_readonly(database)
        try:
            conn.execute('BEGIN')
            rows = [dict(row) for row in conn.execute(TARGET_SQL, (manifest['registered_at'],))]
            history, excluded = causal_history(conn, now)
            for row in sorted(rows, key=lambda r: (parse_utc(r['as_of']), r['id'])):
                if saved >= PARAMETERS['maximum_events']:
                    break
                target = target_record(row, manifest, now)
                if (not target or target['analysis_abstain'] or
                    not 0 <= (now-parse_utc(target['as_of'])).total_seconds() <= PARAMETERS['max_input_age_seconds']
                    or (parse_utc(target['end_at'])-now).total_seconds() < PARAMETERS['minimum_remaining_seconds']):
                    continue
                payload = json.loads(row['payload_json'])
                if not available_input(conn, payload):
                    continue
                target.update(relay=frozen_proxy(payload, 'relay', parse_utc(target['as_of'])),
                              spot=frozen_proxy(payload, 'spot', parse_utc(target['as_of'])))
                if not all(probability(target[k]) for k in ('relay', 'spot')):
                    continue
                name = hashlib.sha256(target['event_id'].encode()).hexdigest()+'.json'
                if (output/'events'/name).exists():
                    continue
                start = time.monotonic()
                record = candidate(target, history)
                finished = utcnow()
                if (finished >= parse_utc(target['end_at']) or
                        (finished-parse_utc(target['as_of'])).total_seconds() > PARAMETERS['max_input_age_seconds']):
                    continue
                record.update(recorded_at=finished.isoformat(), fit_elapsed_seconds=time.monotonic()-start,
                    training_data=training_for(history, target),
                    excluded_history=excluded, manifest_sha256=fingerprint(manifest))
                record['record_sha256'] = fingerprint(record)
                if write_once(output/'events'/name, record):
                    write_once(output/'receipts'/name, {'published_at': utcnow().isoformat(),
                        'record_sha256': record['record_sha256'], 'manifest_sha256': fingerprint(manifest)})
                    saved += 1
                    print(json.dumps({key: record[key] for key in
                        ('prediction_id', 'as_of', 'status', 'learned', 'training_events', 'fit_elapsed_seconds')}), flush=True)
        finally:
            conn.close()
        write_report(output/'status.json', {'status': 'observing', 'checked_at': utcnow().isoformat(),
            'saved_events': saved, 'orders_submitted': 0})
        time.sleep(min(PARAMETERS['poll_seconds'], max(0, until-time.monotonic())))
    write_report(output/'status.json', {'status': status, 'checked_at': utcnow().isoformat(),
        'saved_events': saved, 'orders_submitted': 0})
    print(json.dumps(evaluate(output, database)), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('historical', 'watch', 'evaluate'))
    parser.add_argument('--database', type=Path, default=ROOT/'.polycast/polycast.sqlite3')
    parser.add_argument('--output', type=Path, default=ROOT/'.polycast/proxy-probability-v1-20261003')
    parser.add_argument('--watch-seconds', type=int, default=1100)
    args = parser.parse_args()
    if args.command == 'historical':
        historical(args.database, args.output)
    elif args.command == 'watch':
        watch(args.database, args.output, args.watch_seconds)
    else:
        result = evaluate(args.output, args.database)
        print(json.dumps({key: result[key] for key in ('invalid', 'pending', 'scored', 'metrics')}))


if __name__ == '__main__':
    main()
