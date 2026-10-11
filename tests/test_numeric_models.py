from __future__ import annotations
import unittest
from datetime import datetime, timezone
from experiments.numeric_models import chronological_partitions, feature_vector, logit, predict, fit_logistic, fit_lightgbm


def millis(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000)


class NumericPartitionTests(unittest.TestCase):
    def test_v2_clock_must_be_fresh_and_control_the_declared_decision_cutoff(self):
        from copy import deepcopy
        start = millis("2026-10-07")
        raw = start + 123000
        book = {"bid": .4, "ask": .5, "midpoint": .45, "spread": .1, "top5_imbalance": 0,
                "received_at_ms": raw - 1000, "source_timestamp_ms": start + 119000}
        snapshot = {"feature_version": "btc5m-public-microstructure-v2-clock-checked",
                    "market": {"start_at_ms": start, "end_at_ms": start + 300000},
                    "received_at_ms": raw, "as_of_utc_ms": start + 120000, "paired": True,
                    "clock_reference": {"status": "venues_agree", "offset_ms": -3000,
                                        "uncertainty_ms": 200, "checked_at_local_ms": raw - 5000},
                    "polymarket": {"up": book, "down": book.copy()},
                    "coinbase": {"book": book.copy(), "flow": {key: {"complete": True, "taker_imbalance": 0}
                        for key in ("10s", "30s")}}}
        self.assertEqual(len(feature_vector(snapshot)), 10)
        bad = deepcopy(snapshot)
        bad["coinbase"]["book"]["source_timestamp_ms"] = start + 120001
        with self.assertRaises(ValueError):
            feature_vector(bad)
        bad = deepcopy(snapshot)
        bad["clock_reference"]["checked_at_local_ms"] = raw - 120001
        with self.assertRaises(ValueError):
            feature_vector(bad)

    def test_label_availability_and_embargo_prohibit_training_leakage(self):
        boundaries = tuple(millis(date) for date in ("2026-10-08", "2026-10-09", "2026-10-10"))
        target = boundaries[0]
        rows = [{"event_id": "old", "as_of_ms": target - 3600000, "end_at_ms": target - 3420000,
                 "label_observed_at_ms": target - 3300000},
                {"event_id": "late", "as_of_ms": target - 7200000, "end_at_ms": target - 7020000,
                 "label_observed_at_ms": target + 1},
                {"event_id": "near", "as_of_ms": target - 900000, "end_at_ms": target - 720000,
                 "label_observed_at_ms": target - 600000},
                {"event_id": "future", "as_of_ms": boundaries[2] + 3600000,
                 "end_at_ms": boundaries[2] + 3780000, "label_observed_at_ms": boundaries[2] + 4000000}]
        parts = chronological_partitions(rows, boundaries)
        self.assertEqual([row["event_id"] for row in parts["train"]], ["old"])
        self.assertEqual({row["event_id"] for row in parts["excluded"]}, {"late", "near"})
        self.assertEqual([row["event_id"] for row in parts["holdout"]], ["future"])
        with self.assertRaises(ValueError):
            chronological_partitions(rows + [rows[0]], boundaries)

    def test_future_book_or_incomplete_flow_cannot_become_a_feature(self):
        start = millis("2026-10-07")
        book = {"bid": .4, "ask": .5, "midpoint": .45, "spread": .1, "top5_imbalance": 0,
                "received_at_ms": start + 120000, "source_timestamp_ms": start + 119000}
        flow = {key: {"complete": True, "taker_imbalance": 0} for key in ("10s", "30s")}
        snapshot = {"market": {"start_at_ms": start, "end_at_ms": start + 300000}, "received_at_ms": start + 120000,
                    "paired": True, "polymarket": {"up": book, "down": book.copy()},
                    "coinbase": {"book": book.copy(), "flow": flow}}
        self.assertEqual(len(feature_vector(snapshot)), 10)
        flow["10s"]["complete"] = False
        with self.assertRaises(ValueError):
            feature_vector(snapshot)
        flow["10s"]["complete"] = True
        snapshot["coinbase"]["book"]["source_timestamp_ms"] = start + 120001
        with self.assertRaises(ValueError):
            feature_vector(snapshot)


try:
    import lightgbm
    import scipy
    NUMERIC_AVAILABLE = True
except ImportError:
    NUMERIC_AVAILABLE = False


@unittest.skipUnless(NUMERIC_AVAILABLE, "Requires isolated, pinned numeric environment")
class NumericFitTests(unittest.TestCase):
    def test_zero_increment_returns_the_original_market_probability(self):
        rows = [{"features": [logit(q)] + [0.] * 9, "market_probability": q} for q in (.2, .6, .9)]
        model = {"kind": "logistic", "residual": True, "mean": [0.] * 9, "scale": [1.] * 9, "weights": [0.] * 10}
        for actual, expected in zip(predict(model, rows), (.2, .6, .9)):
            self.assertAlmostEqual(actual, expected, places=12)

    def test_lightgbm_training_offset_must_be_added_once_at_inference(self):
        # No predictive features and exactly the market's outcome frequency.
        # This is an offset-contract test, not a market performance test.
        rows = [{"event_id": str(i), "features": [logit(.6)] + [0.] * 9,
                 "market_probability": .6, "outcome": int(i % 5 < 3)} for i in range(300)]
        model = fit_lightgbm(rows, residual=True)
        self.assertAlmostEqual(predict(model, rows[:1])[0], .6, places=6)

    def test_numeric_models_learn_a_signal_and_preserve_probability_bounds(self):
        rows = [{"event_id": str(i), "features": [logit(.5), (i % 10 - 4.5) / 5] + [0.] * 8,
                 "market_probability": .5, "outcome": int(i % 10 >= 5)} for i in range(300)]
        for fitter in (fit_logistic, fit_lightgbm):
            for residual in (False, True):
                model = fitter(rows, residual=residual)
                values = predict(model, [rows[0], rows[9]])
                self.assertTrue(0 < values[0] < values[1] < 1)


if __name__ == "__main__":
    unittest.main()
