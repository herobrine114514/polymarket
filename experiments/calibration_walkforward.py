"""Chronological, read-only logistic probability remapping for BTC 5m research.

Use .polycast/oss-calibration-venv (scikit-learn==1.9.1). Parameters must come
from the registered manifest. No production forecast, order, or DB is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import warnings
from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.btc5m_model_diagnostics import build_report, probability
from polycast.market import parse_utc

EXPERIMENT = "btc5m-logit-calibration-v1"


def logit(value: float, epsilon: float) -> float:
    value = min(1 - epsilon, max(epsilon, value))
    return math.log(value / (1 - value))


def fit_sigmoid(training: list[dict], parameters: dict) -> dict:
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import LogisticRegression
    model = LogisticRegression(C=parameters["C"], solver=parameters["solver"],
                               max_iter=parameters["max_iter"], random_state=parameters["random_state"])
    features = [[logit(e["model"], parameters["epsilon"])] for e in training]
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        model.fit(features, [e["outcome"] for e in training])
    return {"slope": float(model.coef_[0, 0]), "intercept": float(model.intercept_[0])}


def remap(value: float, fitted: dict, epsilon: float) -> float:
    z = fitted["slope"] * logit(value, epsilon) + fitted["intercept"]
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    exp = math.exp(z)
    return exp / (1 + exp)


def metrics(items: list[dict], fields: tuple[str, ...]) -> dict:
    paired = [e for e in items if e["status"] == "scored" and
              all(probability(e.get(field)) for field in fields)]
    result = {"events": len(paired), "brier": {}, "log_loss": {}}
    for field in fields:
        result["brier"][field] = round(sum((e[field] - e["outcome"]) ** 2 for e in paired) /
                                        len(paired), 6) if paired else None
        result["log_loss"][field] = round(sum(-math.log(max(1e-12, e[field] if e["outcome"]
                                                          else 1 - e[field])) for e in paired) /
                                           len(paired), 6) if paired else None
    return result


def walk_forward(events: list[dict], parameters: dict, fit=fit_sigmoid) -> dict:
    # The extractor enforces exact frozen drafts and pre-outcome analyses.
    # Require unique event IDs again so external input cannot amplify a label.
    if len({e["event_id"] for e in events}) != len(events):
        raise ValueError("Expected one frozen analysis per independent event")
    eligible = [e for e in events if not e["analysis_abstain"] and probability(e["model"])]
    eligible.sort(key=lambda e: (parse_utc(e["as_of"]), e["prediction_id"]))
    output = []
    for target in eligible:
        as_of = parse_utc(target["as_of"])
        key = (target["model_version"], target["prompt_version"])
        embargo_cutoff = as_of - timedelta(minutes=parameters["embargo_minutes"])
        training = [e for e in eligible if e["event_id"] != target["event_id"] and
                    (e["model_version"], e["prompt_version"]) == key and
                    parse_utc(e["end_at"]) <= embargo_cutoff and
                    parse_utc(e["as_of"]) < as_of and
                    parse_utc(e["resolved_at"]) < as_of and
                    parse_utc(e["recorded_at"]) < as_of]
        record = {"event_id": target["event_id"], "prediction_id": target["prediction_id"],
                  "as_of": target["as_of"], "model_version": key[0], "prompt_version": key[1],
                  "model": target["model"], "market": target["market"],
                  "outcome": target["outcome"], "review_approved": target["review_approved"],
                  "training_events": len(training), "training_event_ids": [e["event_id"] for e in training],
                  "training_sha256": hashlib.sha256(json.dumps(training, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
                  "calibrated": None, "fitted": None}
        counts = Counter(e["outcome"] for e in training)
        if len(training) < parameters["min_train_events"]:
            record["status"] = "insufficient_training"
        elif min(counts.get(0, 0), counts.get(1, 0)) < parameters["min_each_outcome"]:
            record["status"] = "insufficient_outcome_coverage"
        else:
            try:
                fitted = fit(training, parameters)
                if (not all(math.isfinite(fitted[k]) for k in ("slope", "intercept")) or
                        fitted["slope"] <= 0):
                    record["status"] = "nonmonotonic_or_invalid_fit"
                else:
                    record["calibrated"] = remap(target["model"], fitted, parameters["epsilon"])
                    record["fitted"] = fitted
                    record["status"] = "scored"
            except Exception as exc:
                record["status"] = "fit_failed"
                # No model text or account information is present; still avoid
                # dumping arbitrary library diagnostics into the artifact.
                record["failure_type"] = type(exc).__name__
        output.append(record)
    groups = {}
    for model, prompt in sorted({(e["model_version"], e["prompt_version"]) for e in output}):
        own = [e for e in output if (e["model_version"], e["prompt_version"]) == (model, prompt)]
        groups.setdefault(model, {})[prompt] = {
            "eligible_events": len(own), "status_counts": dict(Counter(e["status"] for e in own)),
            "raw_calibrated": metrics(own, ("model", "calibrated")),
            "strict_market_subset": metrics(own, ("model", "calibrated", "market")),
        }
    return {"eligible_events": len(eligible), "original_abstentions_excluded": len(events) - len(eligible),
            "status_counts": dict(Counter(e["status"] for e in output)),
            "by_model_and_prompt": groups, "events": output}


def fixture_check(parameters: dict) -> dict:
    # Known synthetic monotonic relationship tests library/feature compatibility,
    # never used to claim live performance or select hyperparameters.
    training = [{"model": .2 if i % 2 == 0 else .8, "outcome": i % 2} for i in range(40)]
    fitted = fit_sigmoid(training, parameters)
    low, high = remap(.2, fitted, parameters["epsilon"]), remap(.8, fitted, parameters["epsilon"])
    if not 0 < low < .5 < high < 1 or fitted["slope"] <= 0:
        raise RuntimeError("Synthetic monotonic fixture failed")
    return {"status": "passed", "kind": "synthetic interface fixture, not market performance",
            "fitted": fitted, "low": low, "high": high}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(".polycast/polycast.sqlite3"))
    parser.add_argument("--manifest", type=Path, default=Path(".polycast/calibration-experiment-v1.json"))
    parser.add_argument("--cutoff", help="Timezone-aware data cutoff; default current UTC")
    parser.add_argument("--output", type=Path, default=Path(".polycast/calibration-walkforward.json"))
    args = parser.parse_args()
    if version("scikit-learn") != "1.9.1":
        raise SystemExit("Use the pinned isolated scikit-learn==1.9.1 environment")
    manifest_raw = args.manifest.read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get("experiment") != EXPERIMENT:
        raise SystemExit("Unexpected experiment manifest")
    parameters = manifest["parameters"]
    now = datetime.now(timezone.utc)
    try:
        cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00")) if args.cutoff else now
        if cutoff.tzinfo is None or cutoff > now:
            raise ValueError("Cutoff must be timezone-aware and not in the future")
        cutoff = cutoff.astimezone(timezone.utc)
    except ValueError as exc:
        parser.error(str(exc))
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        dataset = build_report(conn, cutoff)
    result = walk_forward(dataset["events"], parameters)
    artifact = {"experiment": EXPERIMENT, "generated_at": now.isoformat(timespec="seconds"),
                "cutoff_utc": cutoff.isoformat(timespec="seconds"), "parameters": parameters,
                "manifest_registered_at": manifest["registered_at"],
                "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "data_sha256": dataset["data_sha256"],
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "packages": {name:version(name) for name in ("scikit-learn", "numpy", "scipy")},
                "limitations": [manifest["limits"],
                    "Parameters registered before fitting; historical diagnosis remains exploratory",
                    "No production model probability, strategy or order is changed",
                    "No IID confidence interval, threshold tuning or live admission from these scores"],
                "fixture": fixture_check(parameters), **result}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "cutoff": artifact["cutoff_utc"],
                      "fixture": artifact["fixture"]["status"], "eligible_events": result["eligible_events"],
                      "status_counts": result["status_counts"], "by_model_and_prompt": result["by_model_and_prompt"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
