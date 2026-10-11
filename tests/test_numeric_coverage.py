from __future__ import annotations
import copy
import unittest
from experiments.numeric_coverage import audit, VERSION, PERIOD, TARGET

BASE = 20000 * 86400000


def market(identifier, start):
    return {"market_id": identifier, "event_id": "event-" + identifier, "start_at_ms": start,
            "end_at_ms": start + PERIOD, "up_asset": identifier + "00", "down_asset": identifier + "01", "rules": "rules"}


def snapshot(meta, elapsed, *, paired=True):
    utc = meta["start_at_ms"] + elapsed
    raw = utc + 2700
    book = {"bid": .4, "ask": .5, "spread": .1, "midpoint": .45, "top5_imbalance": 0,
            "received_at_ms": raw - 500, "source_timestamp_ms": utc - 1000}
    return {"kind": "feature_snapshot", "feature_version": VERSION, "market": copy.deepcopy(meta),
            "as_of_utc_ms": utc, "received_at_ms": raw, "paired": paired,
            "clock_reference": {"status": "venues_agree", "offset_ms": -2700, "uncertainty_ms": 100,
                                "checked_at_local_ms": raw - 1000},
            "polymarket": {"up": book.copy() if paired else None, "down": book.copy(),
                "up_quality": {"valid": paired, "reason": None if paired else "one_sided_book"}},
            "coinbase": {"book": book.copy(), "flow": {window:{"complete": True, "taker_imbalance": 0}
                for window in ("10s", "30s")}}}


def envelope(rows):
    return [{**row, "schema_version": 1, "session_id": "session", "record_seq": i + 1} for i, row in enumerate(rows)]


class NumericCoverageTests(unittest.TestCase):
    def test_unusable_windows_remain_in_the_denominator_and_future_windows_do_not(self):
        first, second, future = (market(str(i + 1), BASE + i * PERIOD) for i in range(3))
        rows = envelope([{"kind": "market_selection", "markets": [first, second, future]},
            snapshot(first, TARGET), snapshot(first, TARGET + 4000), snapshot(second, TARGET, paired=False),
            snapshot(second, TARGET + 4000, paired=False)])
        frozen = {"1": ({k:v for k,v in rows[1].items() if k != "schema_version"}, "a" * 64)}
        result = audit(rows, frozen, "session")
        self.assertEqual(result["announced_market_count"], 3)
        self.assertEqual(result["announced_finished_window_count"], 2)
        self.assertEqual(result["input_coverage_among_announced_finished_windows"], .5)
        self.assertEqual(result["status_counts"], {"valid_frozen_input": 1, "window_observed_inputs_unusable": 1,
                                                 "pending_or_outside_verified_prefix": 1})
        self.assertIn("up: one_sided_book", result["events"][1]["reasons"])
        self.assertIsNone(result["model_forecast_or_trade_coverage"])

    def test_expected_missing_slots_are_not_claimed_as_confirmed_market_identities(self):
        first, later = market("1", BASE), market("4", BASE + 3 * PERIOD)
        rows = envelope([{"kind": "market_selection", "markets": [first, later]},
            snapshot(first, TARGET), snapshot(later, TARGET), snapshot(later, TARGET + 4000)])
        result = audit(rows, {}, "session")
        self.assertEqual(result["announced_market_count"], 2)
        self.assertEqual(result["expected_cadence_slot_count"], 4)
        self.assertEqual(result["expected_slots_not_announced"], [BASE + PERIOD, BASE + 2 * PERIOD])
        self.assertEqual(result["status_counts"]["valid_input_without_frozen_record"], 2)
        self.assertFalse(result["profitability_validated"])

    def test_a_frozen_file_cannot_hide_a_different_journal_value_or_sequence_gap(self):
        meta = market("1", BASE)
        rows = envelope([{"kind": "market_selection", "markets": [meta]}, snapshot(meta, TARGET), snapshot(meta, TARGET + 4000)])
        changed = {k:v for k,v in rows[1].items() if k != "schema_version"}
        changed["received_at_ms"] += 1
        result = audit(rows, {"1": (changed, "a" * 64)}, "session")
        self.assertEqual(result["status_counts"], {"invalid_frozen_input": 1})
        self.assertEqual(result["valid_frozen_input_count"], 0)
        rows[1]["record_seq"] = 9
        with self.assertRaisesRegex(ValueError, "dense session"):
            audit(rows, {}, "session")

    def test_failed_clock_near_a_window_remains_a_visible_failure(self):
        meta = market("1", BASE)
        bad = snapshot(meta, TARGET)
        bad.update(as_of_utc_ms=None, clock_reference=None)
        rows = envelope([{"kind": "market_selection", "markets": [meta]}, bad, snapshot(meta, TARGET + 15000)])
        result = audit(rows, {}, "session")
        self.assertEqual(result["status_counts"], {"clock_unverified_near_window": 1})
        self.assertEqual(result["announced_finished_window_count"], 1)
        self.assertEqual(result["input_coverage_among_announced_finished_windows"], 0)
        self.assertEqual(result["clock_counts"]["unverified_or_stale"], 1)


if __name__ == "__main__":
    unittest.main()
