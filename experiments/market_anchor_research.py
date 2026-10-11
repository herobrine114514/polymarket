"""Read-only, market-anchored probability research. Never submits orders.

One registered candidate: shrink the AI/market log-odds difference toward zero.
History is exploratory; only genuinely published future records are prospective.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.btc5m_model_diagnostics import build_report, probability
from experiments.calibration_live_shadow import (
    TARGET_SQL, connect_readonly, evaluate, fingerprint, source_hashes,
    target_record, write_once, write_report,
)
from polycast.market import parse_utc

EXPERIMENT = "btc5m-market-logit-anchor-v1"
MODEL = "deepseek-v4-pro"
PROMPT = "btc5m-v6-explicit-utc-volatility-units"
PARAMETERS = {"ridge_lambda": 10.0, "epsilon": 1e-6, "weight_bounds": [0.0, 1.0],
              "xatol": 1e-8, "maxiter": 500, "gradient_tolerance": 1e-5,
              "min_train_events": 20, "min_each_outcome": 5, "embargo_minutes": 60}
PACKAGES = {"scipy": "1.17.1", "numpy": "2.4.6", "scikit-learn": "1.9.1"}


def utcnow():
    return datetime.now(timezone.utc)


def logit(p, epsilon):
    p = min(1 - epsilon, max(epsilon, p))
    return math.log(p / (1 - p))


def sigmoid(z):
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    value = math.exp(z)
    return value / (1 + value)


def anchored(model, market, weight, epsilon=1e-6):
    if not all(probability(p) for p in (model, market, weight)):
        raise ValueError("Invalid probability or weight")
    if weight == 0:
        return float(market)
    if weight == 1:
        return float(model)
    return sigmoid(logit(market, epsilon) + weight *
                   (logit(model, epsilon) - logit(market, epsilon)))


def training_for(target, history, parameters=PARAMETERS):
    if len({e["event_id"] for e in history}) != len(history):
        raise ValueError("Duplicate training events")
    at = parse_utc(target["as_of"])
    embargo = at - timedelta(minutes=parameters["embargo_minutes"])
    selected = []
    for row in history:
        if (row["event_id"] == target["event_id"] or row["analysis_abstain"] or
                (row["model_version"], row["prompt_version"]) !=
                (target["model_version"], target["prompt_version"]) or
                not probability(row.get("model")) or not probability(row.get("market"))):
            continue
        end, as_of = parse_utc(row["end_at"]), parse_utc(row["as_of"])
        resolved, recorded = parse_utc(row["resolved_at"]), parse_utc(row["recorded_at"])
        if (not all((end, as_of, resolved, recorded)) or not as_of < end <= embargo or
                not resolved < at or not recorded < at):
            continue
        if row["outcome"] not in (0, 1):
            raise ValueError("Invalid training outcome")
        selected.append(row)
    return sorted(selected, key=lambda e: (parse_utc(e["as_of"]), e["prediction_id"]))


def fit_weight(training, parameters=PARAMETERS):
    from scipy.optimize import minimize_scalar
    pairs = [(logit(e["market"], parameters["epsilon"]),
              logit(e["model"], parameters["epsilon"]) - logit(e["market"], parameters["epsilon"]),
              e["outcome"]) for e in training]
    penalty = parameters["ridge_lambda"]
    if not pairs or not math.isfinite(penalty) or penalty <= 0:
        raise ValueError("Invalid fit inputs")

    def objective(weight):
        return sum(max(base + weight * delta, 0) + math.log1p(math.exp(-abs(base + weight * delta))) -
                   label * (base + weight * delta) for base, delta, label in pairs) + penalty * weight ** 2 / 2

    def gradient(weight):
        return sum(delta * (sigmoid(base + weight * delta) - label)
                   for base, delta, label in pairs) + penalty * weight

    # Positive ridge makes the one-dimensional loss strictly convex. Boundary
    # derivatives identify exact endpoints, including an exact market fallback.
    if gradient(0) >= 0:
        weight, method = 0.0, "lower_bound"
    elif gradient(1) <= 0:
        weight, method = 1.0, "upper_bound"
    else:
        result = minimize_scalar(objective, bounds=(0, 1), method="bounded",
                                 options={"xatol": parameters["xatol"], "maxiter": parameters["maxiter"]})
        weight, method = float(result.x), "scipy_bounded"
        if not result.success or not 0 < weight < 1 or abs(gradient(weight)) > parameters["gradient_tolerance"]:
            raise ValueError("Candidate fit did not converge")
    return {"weight": weight, "method": method, "objective": objective(weight),
            "objective_at_market": objective(0), "gradient": gradient(weight)}


def candidate(target, history, parameters=PARAMETERS, fit=fit_weight):
    training = training_for(target, history, parameters)
    record = {**{k: v for k, v in target.items() if k not in ("outcome", "winning_side")},
              "candidate_method": EXPERIMENT, "training_events": len(training),
              "training_event_ids": [e["event_id"] for e in training],
              "training_sha256": fingerprint(training), "calibrated": None, "fitted": None}
    counts = Counter(e["outcome"] for e in training)
    if target["analysis_abstain"]:
        record["status"] = "abstained"
    elif not probability(target.get("market")):
        record["status"] = "missing_strict_market"
    elif len(training) < parameters["min_train_events"]:
        record["status"] = "insufficient_training"
    elif min(counts.get(0, 0), counts.get(1, 0)) < parameters["min_each_outcome"]:
        record["status"] = "insufficient_outcome_coverage"
    else:
        try:
            fitted = fit(training, parameters)
            if not probability(fitted["weight"]):
                raise ValueError("Invalid candidate weight")
            record.update(status="candidate", fitted=fitted, calibrated=anchored(
                target["model"], target["market"], fitted["weight"], parameters["epsilon"]))
        except Exception as exc:
            record.update(status="fit_failed", failure_type=type(exc).__name__)
    return record


def registered_sources():
    return {**source_hashes(ROOT), str(Path(__file__).relative_to(ROOT)).replace("\\", "/"):
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def guard_manifest(database, manifest):
    if (manifest["experiment"] != EXPERIMENT or manifest["parameters"] != PARAMETERS or
            manifest["model_version"] != MODEL or manifest["prompt_version"] != PROMPT or
            manifest["source_hashes"] != registered_sources() or
            manifest["packages"] != {p: version(p) for p in PACKAGES} or
            manifest["database_identity"] != hashlib.sha256(str(database.resolve()).casefold().encode()).hexdigest()):
        raise ValueError("Registered source, model, parameters, dependencies or database changed")


def register(database, output):
    packages = {p: version(p) for p in PACKAGES}
    if packages != PACKAGES:
        raise ValueError("Use the existing pinned calibration environment")
    manifest = {"experiment": EXPERIMENT, "registered_at": utcnow().isoformat(),
                "model_version": MODEL, "prompt_version": PROMPT, "parameters": PARAMETERS,
                "packages": packages, "source_hashes": registered_sources(),
                "database_identity": hashlib.sha256(str(database.resolve()).casefold().encode()).hexdigest(),
                "scope": "Research only; never place or modify orders",
                "historical_selection": "Existing latest eligible frozen nonabstaining analysis per event; strict market pair; exact model/prompt",
                "prospective_selection": "First consistent reviewed forecast after registration, before end/result; retain rejected review and abstentions",
                "method": "sigmoid(logit(market)+w*(logit(model)-logit(market))); w in [0,1], log loss plus 10*w*w/2; no intercept or feature/threshold search",
                "limits": "Designed after inspecting existing performance. Historical tests are exploratory, never prospective credit. No parameter grid, IID intervals or live-admission threshold."}
    if not write_once(output / "manifest.json", manifest):
        raise ValueError("Experiment is already registered; never overwrite")
    return manifest


def historical(database, output, manifest):
    guard_manifest(database, manifest)
    at = utcnow()
    with connect_readonly(database) as conn:
        conn.execute("BEGIN")
        data = build_report(conn, at)
    targets = [e for e in data["events"] if (e["model_version"], e["prompt_version"]) == (MODEL, PROMPT)]
    records = []
    for target in targets:
        record = candidate(target, data["events"])
        record["outcome"] = target["outcome"]  # Only after fitting the prediction.
        records.append(record)
    scored = [r for r in records if r["status"] == "candidate"]
    score = {key: round(sum((r[key] - r["outcome"]) ** 2 for r in scored) / len(scored), 6)
             if scored else None for key in ("model", "market", "calibrated")}
    report = {"experiment": EXPERIMENT, "scope": "Exploratory chronological replay; zero prospective or profit credit",
              "generated_at": at.isoformat(), "manifest_sha256": fingerprint(manifest),
              "data_sha256": data["data_sha256"], "selected_events": len(targets),
              "status_counts": dict(Counter(r["status"] for r in records)),
              "scored_events": len(scored), "brier": score,
              "exact_market_fallbacks": sum(r["fitted"]["weight"] == 0 for r in scored),
              "mean_ai_weight": sum(r["fitted"]["weight"] for r in scored) / len(scored) if scored else None,
              "events": records}
    write_report(output / "historical.json", report)
    return {k: v for k, v in report.items() if k != "events"}


def observe(database, output, manifest, clock=utcnow, fit=fit_weight):
    guard_manifest(database, manifest)
    now, manifest_hash = clock(), fingerprint(manifest)
    with connect_readonly(database) as conn:
        conn.execute("BEGIN")
        rows = [dict(r) for r in conn.execute(TARGET_SQL, (manifest["registered_at"],))]
        history = build_report(conn, now)["events"]
        resolutions = {r["market_id"]: dict(r) for r in conn.execute("SELECT * FROM resolutions")}
    targets = sorted((t for row in rows if (t := target_record(row, manifest, now)) is not None),
                     key=lambda t: (parse_utc(t["as_of"]), t["prediction_id"]))
    for target in targets:
        path = output / "events" / (hashlib.sha256(target["event_id"].encode()).hexdigest() + ".json")
        if path.exists():
            continue
        record = candidate(target, history, fit=fit)
        finished = clock()
        if finished >= parse_utc(target["end_at"]):
            continue
        with connect_readonly(database) as conn:
            if conn.execute("SELECT 1 FROM resolutions WHERE market_id=?", (target["market_id"],)).fetchone():
                continue
        finished = clock()
        if finished >= parse_utc(target["end_at"]):
            continue
        record.update(recorded_at=finished.isoformat(), manifest_sha256=manifest_hash)
        record["record_sha256"] = fingerprint(record)
        if write_once(path, record):
            write_once(output / "receipts" / path.name,
                       {"published_at": clock().isoformat(), "record_sha256": record["record_sha256"],
                        "manifest_sha256": manifest_hash})
    records, malformed = [], 0
    for path in sorted((output / "events").glob("*.json")):
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if path.stem != hashlib.sha256(saved["event_id"].encode()).hexdigest():
                raise ValueError("Record identity mismatch")
            receipt = output / "receipts" / path.name
            saved["publication"] = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else None
            records.append(saved)
        except (ValueError, KeyError, OSError, TypeError, AttributeError):
            malformed += 1
    all_ids, saved_ids = {r["event_id"] for r in rows}, {r["event_id"] for r in records}
    ended_missing = {r["event_id"] for r in rows if r["event_id"] not in saved_ids and
                     parse_utc(r["end_at"]) and parse_utc(r["end_at"]) <= now}
    evaluated_at = clock()
    report = {"experiment": EXPERIMENT, "status": "observing", "checked_at": evaluated_at.isoformat(),
              "manifest_sha256": manifest_hash, "registered_at": manifest["registered_at"],
              "scope": "Prospective probability research; no order or profit credit",
              "calibrated_field_means": "Market-anchored candidate, not proof of calibration",
              **evaluate(records, resolutions, manifest_hash, evaluated_at), "malformed_records": malformed,
              "coverage": {"forecast_events": len(all_ids), "missing_records": len(all_ids - saved_ids),
                           "ended_without_record": len(ended_missing)},
              "latest_candidates": [{k: r.get(k) for k in ["event_id", "prediction_id", "as_of", "end_at",
                                     "recorded_at", "status", "model", "market", "calibrated", "fitted"]}
                                    for r in sorted(records, key=lambda r: r["as_of"])[-3:]]}
    write_report(output / "status.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "historical", "once", "watch"))
    parser.add_argument("--database", type=Path, default=ROOT / ".polycast/polycast.sqlite3")
    parser.add_argument("--output", type=Path, default=ROOT / ".polycast/market-anchor-v1")
    parser.add_argument("--watch-seconds", type=int, default=620)
    args = parser.parse_args()
    if args.command == "register":
        manifest = register(args.database, args.output)
        print(json.dumps({"experiment": EXPERIMENT, "registered_at": manifest["registered_at"]}), flush=True)
        return
    manifest = json.loads((args.output / "manifest.json").read_text(encoding="utf-8"))
    if args.command == "historical":
        print(json.dumps(historical(args.database, args.output, manifest)), flush=True)
        return
    if args.command == "once":
        print(json.dumps(observe(args.database, args.output, manifest)), flush=True)
        return
    if not 10 <= args.watch_seconds <= 1800:
        parser.error("Bounded observation must be between 10 and 1800 seconds")
    until, last = time.monotonic() + args.watch_seconds, None
    while time.monotonic() < until:
        report = observe(args.database, args.output, manifest)
        signature = fingerprint({k: report[k] for k in ["frozen_events", "invalid_records", "pending_events",
                                    "resolved_calibrated_market", "malformed_records", "coverage"]})
        if signature != last:
            print(json.dumps(report), flush=True)
            last = signature
        time.sleep(min(10, max(0, until - time.monotonic())))
    report = observe(args.database, args.output, manifest)
    report["status"] = "bounded_observation_completed"
    write_report(args.output / "status.json", report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
