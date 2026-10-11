"""Offline prototype training/calibration; final holdout is never scored here.

This does not register a prospective cohort, select a production model, submit
orders or establish profitable performance. Run with oss-numeric-venv.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.numeric_models import (FEATURE_NAMES, PARAMETERS, chronological_partitions,
    fit_logistic, fit_lightgbm, fit_sigmoid, predict, logit, feature_names)
from experiments.causal_features import VERSION as CAUSAL_VERSION

VERSION = "btc5m-public-microstructure-v2-clock-checked"
MINIMUMS = {"train": 1000, "calibration": 250, "validation": 250}
MINIMUM_DAYS = {"train": 3, "calibration": 1, "validation": 1}


def checked_inputs(dataset: dict, plan: dict) -> dict:
    version=plan.get('feature_version'); names=feature_names(version)
    if (plan.get("status") != "prototype_design_not_prospective_registration" or
            version not in (VERSION,CAUSAL_VERSION) or plan.get("feature_names") != list(names) or
            plan.get("parameters") != PARAMETERS or plan.get("embargo_ms") != 35 * 60000 or
            plan.get("minimum_events") != MINIMUMS or plan.get("minimum_days") != MINIMUM_DAYS):
        raise ValueError("Unknown prototype design/version/parameters")
    boundaries = plan.get("boundaries_ms")
    start, finish = plan.get("train_start_ms"), plan.get("holdout_end_ms")
    if (type(start) is not int or type(finish) is not int or start % 86400000 or finish % 86400000 or
            not isinstance(boundaries, list) or len(boundaries) != 3 or
            any(type(value) is not int or value % 86400000 for value in boundaries) or
            not start < boundaries[0] < boundaries[1] < boundaries[2] < finish):
        raise ValueError("Require a fixed start, three UTC boundaries and holdout end")
    hashes = plan.get("expected_capture_hashes")
    capture_names={'microstructure_frames.mjs','microstructure_collector.mjs'} | ({'causal_context.mjs'} if version==CAUSAL_VERSION else set())
    if (not isinstance(hashes, dict) or set(hashes) != capture_names or
            any(not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value) for value in hashes.values())):
        raise ValueError("Expected capture hashes required")
    if (dataset.get("dataset_version") != ('btc5m-causal-labels-v3' if version==CAUSAL_VERSION else "btc5m-public-microstructure-labels-v2") or
            dataset.get("requested_feature_version") != version or
            not set(dataset.get("feature_versions", [])) <= {version} or
            version==CAUSAL_VERSION and dataset.get('requested_capture_hashes')!=hashes or
            type(dataset.get("cutoff_ms")) is not int or not isinstance(dataset.get("events"), list)):
        raise ValueError("Dataset must explicitly select checked UTC features")
    cutoff = dataset["cutoff_ms"]
    for row in dataset["events"]:
        if (row.get("feature_version") != version or row.get("capture_source_hashes") != hashes or
                row.get("independent_reference_verified") is not False or
                type(row.get("outcome")) is not int or row["outcome"] not in (0, 1) or
                type(row.get("label_observed_at_ms")) is not int or row["label_observed_at_ms"] > cutoff or
                len(row.get("features", [])) != len(names) or
                any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in row["features"]) or
                isinstance(row.get("market_probability"), bool) or not isinstance(row.get("market_probability"), (int, float)) or
                not 0 < row["market_probability"] < 1 or
                abs(row["features"][0] - logit(row["market_probability"])) > 1e-7):
            raise ValueError("Invalid, late label or mixed capture version")
    selected, outside = [], []
    for row in dataset["events"]:
        if type(row.get("as_of_ms")) is not int:
            raise ValueError("Invalid decision clock")
        if start <= row["as_of_ms"] < finish:
            selected.append(row)
        else:
            outside.append({"event_id": row["event_id"], "reason": "outside_fixed_experiment_dates"})
    partitions = chronological_partitions(selected, tuple(boundaries), embargo_ms=plan["embargo_ms"])
    partitions["excluded"].extend(outside)
    # A validation report must be made after its fixed interval has ended.
    if cutoff < plan["boundaries_ms"][2]:
        return {"partitions": partitions, "interval_complete": False}
    return {"partitions": partitions, "interval_complete": True}


def calibrate(probabilities: list[float], mapping: dict) -> list[float]:
    result = []
    for probability in probabilities:
        z = max(-700, min(700, mapping["intercept"] + mapping["slope"] * logit(probability)))
        result.append(1 / (1 + math.exp(-z)))
    return result


def metrics(probabilities: list[float], rows: list[dict]) -> dict:
    if len(probabilities) != len(rows) or not rows or any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities):
        raise ValueError("Invalid probability evaluation")
    brier, loss = [], []
    bins = [{"count": 0, "probability_sum": 0., "outcome_sum": 0.} for _ in range(10)]
    days = {}
    for probability, row in zip(probabilities, rows):
        y, p = row["outcome"], min(1 - 1e-6, max(1e-6, probability))
        brier.append((probability - y) ** 2)
        loss.append(-y * math.log(p) - (1 - y) * math.log1p(-p))
        bucket = bins[min(9, int(probability * 10))]
        bucket["count"] += 1
        bucket["probability_sum"] += probability
        bucket["outcome_sum"] += y
        day = str(row["as_of_ms"] // 86400000)
        daily = days.setdefault(day, {"events": 0, "brier_sum": 0., "log_loss_sum": 0.})
        daily["events"] += 1
        daily["brier_sum"] += brier[-1]
        daily["log_loss_sum"] += loss[-1]
    return {"events": len(rows), "brier": sum(brier) / len(rows), "log_loss": sum(loss) / len(rows),
            "reliability": [{"lower": i / 10, "upper": (i + 1) / 10, "count": item["count"],
                "mean_probability": item["probability_sum"] / item["count"] if item["count"] else None,
                "observed_rate": item["outcome_sum"] / item["count"] if item["count"] else None} for i, item in enumerate(bins)],
            "utc_day_totals": days}


def run(dataset: dict, plan: dict) -> dict:
    inputs = checked_inputs(dataset, plan)
    partitions = inputs["partitions"]
    counts = {name: len(rows) for name, rows in partitions.items()}
    reasons = []
    for name, minimum in MINIMUMS.items():
        rows = partitions[name]
        if len(rows) < minimum:
            reasons.append(f"{name}: need {minimum} independent events, have {len(rows)}")
        days = len({row["as_of_ms"] // 86400000 for row in rows})
        if days < MINIMUM_DAYS[name]:
            reasons.append(f"{name}: insufficient UTC date coverage")
        if {row["outcome"] for row in rows} != {0, 1}:
            reasons.append(f"{name}: both official outcomes required")
    if not inputs["interval_complete"]:
        reasons.append("fixed validation interval has not ended")
    report = {"schema_version": 1, "experiment": "numeric-offline-prototype-v1", "partition_counts": counts,
              'feature_version':plan['feature_version'],'feature_names':plan['feature_names'],
              "holdout_scored": False, "production_selected": False, "prospective_registered": False,
              "profitability_validated": False, "live_ready": False, "net_profit": None,
              "excluded": partitions["excluded"], "reasons": reasons, "models": {}, "validation": {}}
    if reasons:
        report["status"] = "insufficient_samples_or_interval"
        return report
    train, calibration, validation = (partitions[name] for name in ("train", "calibration", "validation"))
    outcomes = [row["outcome"] for row in calibration]
    base_val = [row["market_probability"] for row in validation]
    base_score = metrics(base_val, validation)
    report["validation"]["market_raw"] = base_score
    mapping = fit_sigmoid([row["market_probability"] for row in calibration], outcomes)
    report["models"]["market_sigmoid"] = {"kind": "market", "calibrator": mapping}
    report["validation"]["market_sigmoid"] = metrics(calibrate(base_val, mapping), validation)
    for kind, fitter in (("logistic", fit_logistic), ("lightgbm", fit_lightgbm)):
        for residual in (False, True):
            name = kind + ("_residual" if residual else "_direct")
            model = fitter(train, residual=residual)
            mapping = fit_sigmoid(predict(model, calibration), outcomes)
            report["models"][name] = {"model": model, "calibrator": mapping}
            probabilities = predict(model, validation)
            report["validation"][name + "_raw"] = metrics(probabilities, validation)
            report["validation"][name + "_sigmoid"] = metrics(calibrate(probabilities, mapping), validation)
    for score in report["validation"].values():
        score["paired_brier_difference_vs_market"] = score["brier"] - base_score["brier"]
        score["paired_log_loss_difference_vs_market"] = score["log_loss"] - base_score["log_loss"]
    report["status"] = "offline_fit_validation_only"
    report["limitations"] = ["Public relay remains an unsigned near-start reference proxy; sampled price paths are not engine paths",
        "No prospective probabilities, final holdout scores, executable replay costs or confidence interval",
        "Official outcome labels do not establish an independent signed oracle reference"]
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data_bytes, plan_bytes = args.dataset.read_bytes(), args.plan.read_bytes()
    report = run(json.loads(data_bytes), json.loads(plan_bytes))
    report.update(dataset_sha256=hashlib.sha256(data_bytes).hexdigest(), plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
                  source_hashes={name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                                 for name in ("numeric_pipeline.py", "numeric_models.py", "numeric_dataset.py", "journal_replay.py",'causal_features.py')})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "partition_counts": report["partition_counts"],
                      "holdout_scored": False, "live_ready": False}))
