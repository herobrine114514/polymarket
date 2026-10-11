from __future__ import annotations
import copy
import importlib.util
import unittest
from unittest.mock import patch

from experiments import numeric_pipeline as pipeline

DAY = 86400000
BASE = 20000 * DAY


def design():
    return {"status": "prototype_design_not_prospective_registration", "feature_version": pipeline.VERSION,
            "feature_names": list(pipeline.FEATURE_NAMES), "parameters": copy.deepcopy(pipeline.PARAMETERS),
            "expected_capture_hashes": {"microstructure_frames.mjs": "a" * 64, "microstructure_collector.mjs": "b" * 64},
            "embargo_ms": 35 * 60000, "minimum_events": pipeline.MINIMUMS.copy(),
            "minimum_days": pipeline.MINIMUM_DAYS.copy(), "boundaries_ms": [BASE + 4 * DAY, BASE + 5 * DAY, BASE + 6 * DAY],
            "train_start_ms": BASE, "holdout_end_ms": BASE + 8 * DAY}


def dataset(plan, counts=(0, 0, 0, 0)):
    rows = []
    for partition, (count, start) in enumerate(zip(counts, [BASE, BASE + 4 * DAY, BASE + 5 * DAY, BASE + 6 * DAY])):
        for i in range(count):
            stamp = start + 120000 + i * 300000
            rows.append({"event_id": f"{partition}:{i}", "as_of_ms": stamp, "end_at_ms": stamp + 180000,
                "label_observed_at_ms": stamp + 300000, "outcome": int(i % 17 < 8),
                "features": [pipeline.logit(.5), (i % 17 - 8) / 16] + [0.] * (len(plan['feature_names'])-2),
                "market_probability": .5, "feature_version": plan['feature_version'],
                "capture_source_hashes": plan["expected_capture_hashes"].copy(), "independent_reference_verified": False})
    return {"dataset_version": 'btc5m-causal-labels-v3' if plan['feature_version']==pipeline.CAUSAL_VERSION else "btc5m-public-microstructure-labels-v2",
            "requested_feature_version": plan['feature_version'],'requested_capture_hashes':plan['expected_capture_hashes'].copy(),
            "feature_versions": [plan['feature_version']] if rows else [], "cutoff_ms": BASE + 7 * DAY, "events": rows}


def causal_design():
    plan=design();plan.update(feature_version=pipeline.CAUSAL_VERSION,feature_names=list(pipeline.feature_names(pipeline.CAUSAL_VERSION)))
    plan['expected_capture_hashes']['causal_context.mjs']='c'*64
    return plan


class NumericPipelineGuardTests(unittest.TestCase):
    def test_v3_requires_explicit_capture_filter_and_32_feature_columns(self):
        plan=causal_design();data=dataset(plan,(2,2,2,2));self.assertEqual(len(data['events'][0]['features']),32)
        self.assertEqual(pipeline.run(data,plan)['status'],'insufficient_samples_or_interval')
        for field in ('requested_capture_hashes','requested_feature_version'):
            bad=copy.deepcopy(data);bad.pop(field)
            with self.assertRaises(ValueError):pipeline.run(bad,plan)
    def test_insufficient_data_never_invokes_training_or_calibration(self):
        plan = design()
        with patch.object(pipeline, "fit_logistic") as logistic, patch.object(pipeline, "fit_lightgbm") as tree, patch.object(pipeline, "fit_sigmoid") as calibration:
            result = pipeline.run(dataset(plan, (2, 2, 2, 2)), plan)
        for function in (logistic, tree, calibration):
            function.assert_not_called()
        self.assertEqual(result["status"], "insufficient_samples_or_interval")
        self.assertFalse(result["holdout_scored"])
        self.assertFalse(result["live_ready"])

    def test_source_revision_future_label_and_modified_design_are_rejected(self):
        plan = design()
        data = dataset(plan, (2, 2, 2, 2))
        changed = copy.deepcopy(data)
        changed["events"][0]["capture_source_hashes"]["microstructure_frames.mjs"] = "c" * 64
        with self.assertRaises(ValueError):
            pipeline.run(changed, plan)
        changed = copy.deepcopy(data)
        changed["events"][0]["label_observed_at_ms"] = data["cutoff_ms"] + 1
        with self.assertRaises(ValueError):
            pipeline.run(changed, plan)
        changed = copy.deepcopy(plan)
        changed["parameters"]["rounds"] += 1
        with self.assertRaises(ValueError):
            pipeline.run(data, changed)

    def test_score_and_reliability_keep_event_counts_and_endpoint_log_loss(self):
        rows = [{"outcome": 0, "as_of_ms": BASE}, {"outcome": 1, "as_of_ms": BASE + DAY}]
        score = pipeline.metrics([.1, .9], rows)
        self.assertAlmostEqual(score["brier"], .01)
        self.assertEqual(sum(row["count"] for row in score["reliability"]), 2)
        self.assertEqual(len(score["utc_day_totals"]), 2)
        self.assertTrue(pipeline.metrics([0., 1.], rows)["log_loss"] > 0)


@unittest.skipUnless(importlib.util.find_spec("lightgbm") and importlib.util.find_spec("scipy"), "isolated numeric environment required")
class NumericPipelineFitTests(unittest.TestCase):
    def test_v3_complete_feature_model_fit_preserves_holdout(self):
        plan=causal_design();result=pipeline.run(dataset(plan,(1000,250,250,2)),plan)
        self.assertEqual(result['status'],'offline_fit_validation_only')
        self.assertEqual(result['feature_version'],pipeline.CAUSAL_VERSION)
        self.assertEqual(len(result['feature_names']),32);self.assertFalse(result['holdout_scored'])
        self.assertEqual(len(result['validation']),10)

    def test_training_calibration_and_validation_do_not_consume_holdout(self):
        # Synthetic contract data only; this does not test real forecasting performance.
        plan = design()
        data = dataset(plan, (1000, 250, 250, 2))
        trained, evaluated = [], []
        actual_logistic, actual_tree, actual_metrics = pipeline.fit_logistic, pipeline.fit_lightgbm, pipeline.metrics

        def logistic(rows, **kwargs):
            trained.extend(row["event_id"] for row in rows)
            return actual_logistic(rows, **kwargs)

        def tree(rows, **kwargs):
            trained.extend(row["event_id"] for row in rows)
            return actual_tree(rows, **kwargs)

        def scores(probabilities, rows):
            evaluated.extend(row["event_id"] for row in rows)
            return actual_metrics(probabilities, rows)

        with patch.object(pipeline, "fit_logistic", side_effect=logistic), patch.object(pipeline, "fit_lightgbm", side_effect=tree), patch.object(pipeline, "metrics", side_effect=scores):
            result = pipeline.run(data, plan)
        self.assertEqual(result["status"], "offline_fit_validation_only")
        self.assertTrue(all(identifier.startswith("0:") for identifier in trained))
        self.assertTrue(all(identifier.startswith("2:") for identifier in evaluated))
        self.assertEqual(result["partition_counts"], {"train": 1000, "calibration": 250, "validation": 250, "holdout": 2, "excluded": 0})
        self.assertFalse(result["holdout_scored"])
        self.assertEqual(len(result["models"]), 5)
        self.assertEqual(len(result["validation"]), 10)
        self.assertTrue(all(item["events"] == 250 for item in result["validation"].values()))


if __name__ == "__main__":
    unittest.main()
