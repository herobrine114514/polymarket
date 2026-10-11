from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from experiments.calibration_walkforward import metrics, remap, walk_forward


PARAMETERS = {"C": 1.0, "solver": "lbfgs", "max_iter": 1000, "random_state": 0,
              "epsilon": 1e-6, "min_train_events": 20, "min_each_outcome": 5,
              "embargo_minutes": 60}


def event(number, end, outcome=None, **updates):
    end = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    result = {"event_id": str(number), "prediction_id": number,
              "as_of": (end - timedelta(minutes=4)).isoformat(),
              "end_at": end.isoformat(),
              "resolved_at": (end + timedelta(minutes=2)).isoformat(),
              "recorded_at": (end + timedelta(minutes=3)).isoformat(),
              "model_version": "test-model", "prompt_version": "test-prompt",
              "analysis_abstain": False, "review_approved": True,
              "model": .7, "market": .6,
              "outcome": number % 2 if outcome is None else outcome}
    result.update(updates)
    return result


def history():
    first = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    return [event(i, (first + timedelta(minutes=5 * i)).isoformat()) for i in range(20)]


class CalibrationWalkForwardTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fit(self, training, parameters):
        self.calls.append(copy.deepcopy(training))
        return {"slope": .8, "intercept": -.1}

    def test_embargo_available_labels_and_exact_versions_restrict_training(self):
        target = event(100, "2026-09-01T12:05:00")
        excluded = [
            event(20, "2026-09-01T11:02:00"),  # Less than an hour before forecast.
            event(21, "2026-09-01T10:00:00", recorded_at=target["as_of"]),
            event(22, "2026-09-01T10:05:00", resolved_at="2026-09-01T12:02:00Z"),
            event(23, "2026-09-01T10:10:00", model_version="another-model"),
            event(24, "2026-09-01T10:15:00", prompt_version="another-prompt"),
            event(25, "2026-09-01T10:20:00", analysis_abstain=True),
            event(26, "2026-09-01T13:05:00"),  # Future forecast and outcome.
        ]
        result = walk_forward(list(reversed(history() + excluded + [target])), PARAMETERS, self.fit)
        scored = next(e for e in result["events"] if e["event_id"] == "100")
        self.assertEqual(scored["status"], "scored")
        self.assertEqual(set(scored["training_event_ids"]), {str(i) for i in range(20)})
        self.assertEqual(scored["training_events"], 20)

    def test_unavailable_and_target_outcomes_cannot_change_candidate(self):
        target = event(100, "2026-09-01T12:05:00", outcome=1)
        unavailable = event(20, "2026-09-01T10:05:00",
                            recorded_at="2026-09-01T12:02:00Z", outcome=1)
        data = history() + [unavailable, target]
        original = walk_forward(data, PARAMETERS, self.fit)["events"][-1]
        mutated = copy.deepcopy(data)
        mutated[-1]["outcome"] = 0
        mutated[-2]["outcome"] = 0
        changed = walk_forward(mutated, PARAMETERS, self.fit)["events"][-1]
        self.assertEqual(original["calibrated"], changed["calibrated"])
        self.assertEqual(original["training_sha256"], changed["training_sha256"])
        self.assertNotEqual(metrics([original], ("calibrated",)),
                            metrics([changed], ("calibrated",)))
        self.assertEqual(data[-1]["outcome"], 1)

    def test_sample_and_outcome_coverage_gates_do_not_fit(self):
        target = event(100, "2026-09-01T12:05:00")
        result = walk_forward(history()[:19] + [target], PARAMETERS, self.fit)
        self.assertEqual(result["events"][-1]["status"], "insufficient_training")
        one_sided = history()
        for e in one_sided:
            e["outcome"] = 1
        result = walk_forward(one_sided + [target], PARAMETERS, self.fit)
        self.assertEqual(result["events"][-1]["status"], "insufficient_outcome_coverage")
        self.assertEqual(self.calls, [])

    def test_duplicate_events_and_invalid_fit_cannot_emit_probability(self):
        data = history()
        with self.assertRaises(ValueError):
            walk_forward(data + [copy.deepcopy(data[0])], PARAMETERS, self.fit)
        target = event(100, "2026-09-01T12:05:00")
        for slope in (0, -1, float("nan"), float("inf")):
            with self.subTest(slope=slope):
                result = walk_forward(data + [target], PARAMETERS,
                                      lambda _t, _p: {"slope": slope, "intercept": 0})
                self.assertIsNone(result["events"][-1]["calibrated"])
                self.assertEqual(result["events"][-1]["status"], "nonmonotonic_or_invalid_fit")

    def test_strict_market_scores_use_same_events_and_extreme_logits_are_finite(self):
        result = walk_forward(history() + [event(100, "2026-09-01T12:05:00", market=None)],
                              PARAMETERS, self.fit)
        group = result["by_model_and_prompt"]["test-model"]["test-prompt"]
        self.assertEqual(group["raw_calibrated"]["events"], 1)
        self.assertEqual(group["strict_market_subset"]["events"], 0)
        self.assertEqual(remap(0, {"slope": 1000, "intercept": 0}, 1e-6), 0)
        self.assertEqual(remap(1, {"slope": 1000, "intercept": 0}, 1e-6), 1)


if __name__ == "__main__":
    unittest.main()
