import copy
import importlib.util
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from experiments.market_anchor_research import anchored, candidate, fit_weight, training_for, observe
from experiments.calibration_live_shadow import evaluate, fingerprint


class MarketAnchorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 2, 5, tzinfo=timezone.utc)
        self.target = {"event_id": "target", "market_id": "target-market", "prediction_id": 99,
                       "as_of": self.now.isoformat(), "end_at": (self.now + timedelta(minutes=3)).isoformat(),
                       "reviewed_at": (self.now + timedelta(seconds=5)).isoformat(),
                       "model_version": "pro", "prompt_version": "v6", "analysis_abstain": False,
                       "review_approved": False, "model": .8, "market": .5}
        self.history = [{"event_id": str(i), "prediction_id": i, "model_version": "pro", "prompt_version": "v6",
                         "as_of": (self.now - timedelta(hours=3, minutes=i)).isoformat(),
                         "end_at": (self.now - timedelta(hours=2, minutes=i)).isoformat(),
                         "resolved_at": (self.now - timedelta(minutes=90)).isoformat(),
                         "recorded_at": (self.now - timedelta(minutes=89)).isoformat(),
                         "analysis_abstain": False, "review_approved": False,
                         "model": .8 if i % 2 else .2, "market": .5, "outcome": i % 2} for i in range(20)]

    def test_rejected_reviews_are_retained_without_trade_credit(self):
        result = candidate(self.target, self.history, fit=lambda *_: {"weight": .25})
        self.assertEqual(result["status"], "candidate")
        self.assertFalse(result["review_approved"])
        self.assertEqual(result["training_events"], 20)
        self.assertNotIn("order_id", result)

    @unittest.skipUnless(importlib.util.find_spec("scipy"), "Real optimizer runs in the pinned research environment")
    def test_real_optimizer_finds_interior_bounded_weight(self):
        fitted = fit_weight(self.history)
        self.assertEqual(fitted["method"], "scipy_bounded")
        self.assertTrue(0 < fitted["weight"] < 1)
        self.assertLess(abs(fitted["gradient"]), 1e-5)
        self.assertLess(fitted["objective"], fitted["objective_at_market"])

    @unittest.skipUnless(importlib.util.find_spec("scipy"), "Real optimizer runs in the pinned research environment")
    def test_wrong_ai_signal_chooses_exact_market_fallback(self):
        history = copy.deepcopy(self.history)
        for row in history:
            row["model"] = 1 - row["model"]
        self.assertEqual(fit_weight(history)["weight"], 0)
        self.assertEqual(anchored(.8, .45, 0), .45)

    def test_target_future_outcome_does_not_change_candidate(self):
        one, zero = dict(self.target, outcome=1), dict(self.target, outcome=0)
        fitted = lambda *_: {"weight": .25}
        self.assertEqual(candidate(one, self.history, fit=fitted), candidate(zero, self.history, fit=fitted))

    def test_late_label_receipt_and_embargo_are_excluded(self):
        history = copy.deepcopy(self.history)
        history[0]["recorded_at"] = (self.now + timedelta(seconds=1)).isoformat()
        history[1]["resolved_at"] = self.now.isoformat()
        history[2]["end_at"] = (self.now - timedelta(minutes=59)).isoformat()
        selected = training_for(self.target, history)
        self.assertEqual(len(selected), 17)
        self.assertFalse({"0", "1", "2"} & {r["event_id"] for r in selected})

    def test_different_versions_missing_market_and_abstention_are_excluded(self):
        history = copy.deepcopy(self.history)
        history[0]["model_version"] = "flash"
        history[1]["prompt_version"] = "v5"
        history[2]["market"] = None
        history[3]["analysis_abstain"] = True
        self.assertEqual(len(training_for(self.target, history)), 16)

    def test_duplicate_events_are_not_training_weight(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            training_for(self.target, self.history + [self.history[0]])

    def test_insufficient_training_or_outcomes_do_not_emit_candidate(self):
        self.assertEqual(candidate(self.target, self.history[:-1])["status"], "insufficient_training")
        history = [dict(row, outcome=1) for row in self.history]
        self.assertEqual(candidate(self.target, history)["status"], "insufficient_outcome_coverage")

    def test_missing_target_market_and_raw_abstention_do_not_emit_candidate(self):
        self.assertEqual(candidate(dict(self.target, market=None), self.history)["status"], "missing_strict_market")
        self.assertEqual(candidate(dict(self.target, analysis_abstain=True), self.history)["status"], "abstained")

    def test_invalid_probability_or_fit_fails_closed(self):
        with self.assertRaises(ValueError):
            anchored(float("nan"), .5, .5)
        result = candidate(self.target, self.history, fit=lambda *_: {"weight": 1.5})
        self.assertEqual(result["status"], "fit_failed")
        self.assertIsNone(result["calibrated"])

    def test_new_method_uses_real_publication_receipt_and_outcome_gate(self):
        record = candidate(self.target, self.history, fit=lambda *_: {"weight": .25})
        record.update(manifest_sha256="manifest", recorded_at=(self.now + timedelta(seconds=10)).isoformat())
        record["record_sha256"] = fingerprint(record)
        record["publication"] = {"manifest_sha256": "manifest", "record_sha256": record["record_sha256"],
                                 "published_at": (self.now + timedelta(seconds=11)).isoformat()}
        result = {"target-market": {"winning_side": "YES", "resolved_at": (self.now + timedelta(minutes=4)).isoformat(),
                                    "recorded_at": (self.now + timedelta(minutes=4, seconds=1)).isoformat()}}
        at = self.now + timedelta(minutes=5)
        self.assertEqual(evaluate([record], result, "manifest", at)["resolved_calibrated_market"]["events"], 1)
        bad = copy.deepcopy(record)
        bad["publication"]["published_at"] = (self.now + timedelta(minutes=4)).isoformat()
        self.assertEqual(evaluate([bad], result, "manifest", at)["invalid_records"], 1)
        bad = copy.deepcopy(record)
        bad.pop("publication")
        self.assertEqual(evaluate([bad], result, "manifest", at)["resolved_calibrated_market"]["events"], 0)

    def test_observer_evaluates_after_actual_publication_not_cycle_start(self):
        class EmptyResult:
            def __init__(self, rows=()):
                self.rows = rows
            def __iter__(self):
                return iter(self.rows)
            def fetchone(self):
                return None
        target = self.target
        class FakeConnection:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def execute(self, sql, *args):
                return EmptyResult([target] if 'LEFT JOIN analysis_drafts' in sql else ())
        moments = iter(self.now + timedelta(seconds=i) for i in [0, 10, 11, 12, 13])
        manifest = {"registered_at": (self.now - timedelta(seconds=5)).isoformat()}
        with tempfile.TemporaryDirectory() as directory, \
                patch('experiments.market_anchor_research.guard_manifest'), \
                patch('experiments.market_anchor_research.connect_readonly', return_value=FakeConnection()), \
                patch('experiments.market_anchor_research.target_record', return_value=target), \
                patch('experiments.market_anchor_research.build_report', return_value={"events": self.history}):
            result = observe(Path(directory)/'read-only.db', Path(directory), manifest,
                             clock=lambda: next(moments), fit=lambda *_: {"weight": .25})
        self.assertEqual(result['frozen_events'], 1)
        self.assertEqual(result['invalid_records'], 0)
        self.assertEqual(result['pending_events'], 1)


if __name__ == "__main__":
    unittest.main()
