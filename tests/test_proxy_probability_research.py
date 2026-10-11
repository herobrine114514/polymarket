from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from experiments.proxy_probability_research import (candidate, features, metrics,
    predict_prices, publication_valid, training_for, available_input)
from experiments.calibration_live_shadow import fingerprint

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def event(index, at=NOW):
    return {'event_id': str(index), 'prediction_id': index, 'as_of': at.isoformat(),
        'end_at': (at+timedelta(minutes=4)).isoformat(),
        'resolved_at': (at+timedelta(minutes=5)).isoformat(),
        'recorded_at': (at+timedelta(minutes=6)).isoformat(),
        'model_version': 'deepseek-v4-pro', 'prompt_version': 'btc5m-v6-explicit-utc-volatility-units',
        'outcome': index % 2, 'relay': .8 if index % 2 else .2,
        'spot': .7 if index % 2 else .3, 'model': .51, 'market': .5}


class EmpiricalProbabilityTests(unittest.TestCase):
    def setUp(self):
        self.target = event(100, NOW+timedelta(hours=2))
        self.history = [event(i) for i in range(20)]
        self.fit = lambda training: {'coefficients': [.4, .6], 'intercept': .1}

    def test_training_excludes_late_results_receipts_target_and_embargo(self):
        cases = []
        for i, key in enumerate(('resolved_at', 'recorded_at', 'as_of')):
            cases.append({**event(30+i), key: self.target['as_of']})
        cases += [{**event(40), 'end_at': (NOW+timedelta(minutes=61)).isoformat()},
                  {**event(100), 'event_id': self.target['event_id']}]
        self.assertEqual(len(training_for(self.history+cases, self.target)), 20)

    def test_version_mismatch_never_enters_training(self):
        wrong = {**event(21), 'prompt_version': 'older'}
        self.assertEqual(len(training_for(self.history+[wrong], self.target)), 20)

    def test_minimum_and_both_outcomes_are_required(self):
        self.assertEqual(candidate(self.target, self.history[:19], self.fit)['status'], 'insufficient_training')
        one_side = [{**row, 'outcome': 1} for row in self.history]
        self.assertEqual(candidate(self.target, one_side, self.fit)['status'], 'insufficient_outcome_coverage')

    def test_ai_market_and_target_outcome_do_not_change_probability(self):
        altered = {**self.target, 'model': .01, 'market': .99, 'outcome': 1}
        first = candidate(self.target, self.history, self.fit)
        second = candidate(altered, self.history, self.fit)
        self.assertEqual(first['learned'], second['learned'])
        self.assertEqual(first['training_sha256'], second['training_sha256'])

    def test_missing_features_and_nonfinite_fits_do_not_guess(self):
        for value in (None, float('nan'), True, -1, 2):
            with self.assertRaises(ValueError):
                features({**self.target, 'relay': value})
        for fitted in ({'coefficients': [-1, -1], 'intercept': 0},
                       {'coefficients': [.4], 'intercept': 0},
                       {'coefficients': [.4, .6], 'intercept': float('nan')}):
            self.assertEqual(candidate(self.target, self.history, lambda t: fitted)['status'], 'fit_failed')

    def test_fit_failure_remains_missing(self):
        def failure(training):
            raise RuntimeError('unavailable')
        item = candidate(self.target, self.history, failure)
        self.assertEqual(item['status'], 'fit_failed')
        self.assertIsNone(item['learned'])

    def test_forecast_is_monotone_for_joint_price_increase(self):
        low = predict_prices({'relay': .2, 'spot': .2}, self.fit([]))
        high = predict_prices({'relay': .8, 'spot': .8}, self.fit([]))
        self.assertLess(low, high)

    def test_evidence_requires_all_recorded_availability_times(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('CREATE TABLE evidence(id INTEGER,fetched_at TEXT)')
            conn.execute('INSERT INTO evidence VALUES(1,?)', (NOW.isoformat(),))
            payload = {'as_of': (NOW+timedelta(milliseconds=200)).isoformat(), 'evidence':
                [{'id': 1, 'published_at': NOW.isoformat(), 'first_seen_at': NOW.isoformat()}]}
            self.assertTrue(available_input(conn, payload))
            for key in ('published_at', 'first_seen_at'):
                changed = copy.deepcopy(payload)
                changed['evidence'][0][key] = (NOW+timedelta(seconds=1)).isoformat()
                self.assertFalse(available_input(conn, changed))
            conn.execute('UPDATE evidence SET fetched_at=?', ((NOW+timedelta(seconds=1)).isoformat(),))
            self.assertFalse(available_input(conn, payload))

    def test_publication_cannot_be_late_tampered_or_have_wrong_manifest(self):
        manifest = {'registered_at': NOW.isoformat()}
        item = {'as_of': (NOW+timedelta(seconds=1)).isoformat(),
            'reviewed_at': (NOW+timedelta(seconds=4)).isoformat(),
            'recorded_at': (NOW+timedelta(seconds=5)).isoformat(),
            'end_at': (NOW+timedelta(minutes=5)).isoformat(), 'manifest_sha256': fingerprint(manifest)}
        item['record_sha256'] = fingerprint(item)
        receipt = {'published_at': (NOW+timedelta(seconds=6)).isoformat(),
            'record_sha256': item['record_sha256'], 'manifest_sha256': fingerprint(manifest)}
        self.assertTrue(publication_valid(item, receipt, manifest))
        self.assertFalse(publication_valid({**item, 'model': .9}, receipt, manifest))
        self.assertFalse(publication_valid(item, {**receipt, 'manifest_sha256': 'wrong'}, manifest))
        self.assertFalse(publication_valid(item, {**receipt, 'published_at': item['end_at']}, manifest))
        self.assertFalse(publication_valid(item, {**receipt, 'published_at':
            (NOW+timedelta(seconds=32)).isoformat()}, manifest))
        self.assertFalse(publication_valid(item, receipt, manifest, NOW+timedelta(seconds=5)))

    def test_metrics_use_identical_complete_events_and_exclude_direction_ties(self):
        a = {**self.target, 'status': 'candidate', 'learned': .5, 'outcome': 1}
        missing = {**a, 'market': None}
        result = metrics([a, missing])
        self.assertEqual(result['same_events'], 1)
        self.assertEqual(result['brier']['learned'], .25)
        self.assertEqual(result['directional_events']['learned'], 0)

    def test_native_pinned_fit_with_both_price_features(self):
        python = ROOT/'.polycast/oss-calibration-venv/Scripts/python.exe'
        if not python.exists():
            self.skipTest('Research runtime not installed')
        script = ('from experiments.proxy_probability_research import fit_prices,predict_prices\n'
            'data=[{"event_id":str(i),"relay":.8 if i%2 else .2,"spot":.7 if i%2 else .3,"outcome":i%2} for i in range(20)]\n'
            'fit=fit_prices(data)\n'
            'assert predict_prices({"relay":.8,"spot":.7},fit)>.5\n'
            'assert predict_prices({"relay":.2,"spot":.3},fit)<.5\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'check.py'
            path.write_text(script, encoding='utf-8')
            result = subprocess.run([str(python), '-c', script], cwd=ROOT,
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
