"""Audit input coverage among every market announced in a verified journal prefix.

This counts inputs, not new-model forecasts or trades. Expected cadence slots
are explicitly separate from confirmed market identities. No active logs, API
calls, training, labels or historical prediction changes.
"""
from __future__ import annotations
import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.journal_replay import session_records
from experiments.numeric_dataset import read_decision
from experiments.numeric_models import feature_vector
from experiments.causal_features import VERSION as CAUSAL_VERSION

VERSION = "btc5m-public-microstructure-v2-clock-checked"
PERIOD = 300000
TARGET, TOLERANCE = 120000, 3000


def checked_utc(record: dict) -> int:
    raw, utc, clock = record.get("received_at_ms"), record.get("as_of_utc_ms"), record.get("clock_reference") or {}
    offset, uncertainty, checked = clock.get("offset_ms"), clock.get("uncertainty_ms"), clock.get("checked_at_local_ms")
    if (type(raw) is not int or type(utc) is not int or type(checked) is not int or
            clock.get("status") != "venues_agree" or not 0 <= raw - checked <= 120000 or
            isinstance(offset, bool) or not isinstance(offset, (int, float)) or not math.isfinite(offset) or abs(offset) > 10000 or
            isinstance(uncertainty, bool) or not isinstance(uncertainty, (int, float)) or not math.isfinite(uncertainty) or not 0 <= uncertainty <= 750 or
            abs(raw + offset - utc) > 1):
        raise ValueError("No fresh checked UTC for coverage")
    return utc


def audit(records, frozen: dict[str, tuple[dict, str]], session_id: str, *, feature_version=VERSION) -> dict:
    if feature_version not in (VERSION,CAUSAL_VERSION):raise ValueError('Unknown coverage version')
    events, versions, clocks = {}, Counter(), Counter()
    first_utc, last_utc, last_seq = None, None, 0
    found_frozen, invalid_frozen = {}, {}
    for record in records:
        if record.get("session_id") != session_id or record.get("record_seq") != last_seq + 1:
            raise ValueError("Coverage requires a verified dense session prefix")
        last_seq += 1
        if record.get("kind") == "market_selection":
            for market in record["markets"]:
                key = market["market_id"]
                if (not isinstance(key, str) or not key.isdigit() or type(market.get("start_at_ms")) is not int or
                        type(market.get("end_at_ms")) is not int or market["start_at_ms"] % PERIOD or
                        market["end_at_ms"] - market["start_at_ms"] != PERIOD):
                    raise ValueError("Unverified five-minute cadence/market identity")
                if key in events and events[key]["market"] != market:
                    raise ValueError("Announced market metadata changed inside capture session")
                events.setdefault(key, {"market": market, "announced_seq": last_seq, "first_snapshot_utc": None,
                    "window_snapshots": 0, "valid_window_snapshots": 0, "unverified_clock_near_window": 0, "reasons": Counter()})
        if record.get("kind") != "feature_snapshot":
            continue
        versions[record.get("feature_version", "missing")] += 1
        if record.get("feature_version") != feature_version:
            continue
        key = record["market"]["market_id"]
        if key not in events or events[key]["market"] != record["market"]:
            raise ValueError("Feature market was not announced with this identity")
        item = events[key]
        try:
            utc = checked_utc(record)
        except (ValueError, TypeError):
            clocks["unverified_or_stale"] += 1
            raw = record.get("received_at_ms")
            if type(raw) is int and TARGET - 10000 <= raw - item["market"]["start_at_ms"] <= TARGET + TOLERANCE + 10000:
                item["unverified_clock_near_window"] += 1
            continue
        clocks["verified"] += 1
        first_utc = utc if first_utc is None else min(first_utc, utc)
        last_utc = utc if last_utc is None else max(last_utc, utc)
        item["first_snapshot_utc"] = utc if item["first_snapshot_utc"] is None else min(item["first_snapshot_utc"], utc)
        if key in frozen and frozen[key][0].get("record_seq") == last_seq:
            snapshot, digest = frozen[key]
            if snapshot != {k: v for k, v in record.items() if k != "schema_version"}:
                invalid_frozen[key] = "frozen file differs from verified journal"
            else:
                try:
                    feature_vector(snapshot)
                    found_frozen[key] = {"record_seq": last_seq, "snapshot_sha256": digest}
                except (ValueError, TypeError, KeyError) as exc:
                    invalid_frozen[key] = str(exc)
        elapsed = utc - record["market"]["start_at_ms"]
        if not TARGET <= elapsed <= TARGET + TOLERANCE:
            continue
        item["window_snapshots"] += 1
        try:
            feature_vector(record)
            item["valid_window_snapshots"] += 1
        except (ValueError, TypeError, KeyError) as exc:
            item["reasons"]["feature_check: " + str(exc)] += 1
        pm, cb = record.get("polymarket") or {}, record.get("coinbase") or {}
        for name, quality in (("up", pm.get("up_quality")), ("down", pm.get("down_quality")), ("coinbase", cb.get("book_quality"))):
            if quality and quality.get("valid") is not True:
                item["reasons"][name + ": " + str(quality.get("reason"))] += 1
        for window in ("10s", "30s"):
            if (cb.get("flow") or {}).get(window, {}).get("complete") is not True:
                item["reasons"]["incomplete_flow_" + window] += 1
    results, statuses = [], Counter()
    for key, item in sorted(events.items(), key=lambda pair: (pair[1]["market"]["start_at_ms"], pair[0])):
        start = item["market"]["start_at_ms"]
        close = start + TARGET + TOLERANCE
        if key in invalid_frozen:
            status = "invalid_frozen_input"
        elif key in found_frozen:
            status = "valid_frozen_input"
        elif last_utc is None or close > last_utc:
            status = "pending_or_outside_verified_prefix"
        elif item["unverified_clock_near_window"]:
            status = "clock_unverified_near_window"
        elif first_utc is not None and close < first_utc:
            status = "window_before_verified_capture"
        elif item["first_snapshot_utc"] is not None and item["first_snapshot_utc"] > close:
            status = "late_market_observation"
        elif item["valid_window_snapshots"]:
            status = "valid_input_without_frozen_record"
        elif item["window_snapshots"]:
            status = "window_observed_inputs_unusable"
        else:
            status = "window_not_observed"
        statuses[status] += 1
        results.append({"market_id": key, "event_id": item["market"]["event_id"], "start_at_ms": start,
            "status": status, "window_snapshots": item["window_snapshots"],
            "valid_window_snapshots": item["valid_window_snapshots"], "reasons": dict(item["reasons"]),
            "unverified_clock_near_window": item["unverified_clock_near_window"],
            "frozen": found_frozen.get(key), "invalid_frozen_reason": invalid_frozen.get(key)})
    observed_starts = {item["market"]["start_at_ms"] for item in events.values()}
    expected = []
    if first_utc is not None and last_utc is not None:
        lower = ((first_utc - TARGET + PERIOD - 1) // PERIOD) * PERIOD
        upper = ((last_utc - TARGET - TOLERANCE) // PERIOD) * PERIOD
        expected = list(range(lower, upper + 1, PERIOD))
    finished = sum(statuses[name] for name in ("valid_frozen_input", "invalid_frozen_input", "late_market_observation",
                  "valid_input_without_frozen_record", "window_observed_inputs_unusable", "window_not_observed", "clock_unverified_near_window"))
    duplicates = Counter(item["market"]["start_at_ms"] for item in events.values())
    return {"schema_version": 1, "status": "verified_prefix_input_coverage", "session_id": session_id,
        "feature_version": feature_version, "last_verified_record_seq": last_seq, "first_verified_utc_ms": first_utc,
        "last_verified_utc_ms": last_utc, "clock_counts": dict(clocks), "feature_version_counts": dict(versions),
        "announced_market_count": len(events), "announced_finished_window_count": finished,
        "valid_frozen_input_count": statuses["valid_frozen_input"],
        "input_coverage_among_announced_finished_windows": statuses["valid_frozen_input"] / finished if finished else None,
        "status_counts": dict(statuses), "events": results,
        "expected_cadence_slot_count": len(expected), "expected_slots_not_announced": [stamp for stamp in expected if stamp not in observed_starts],
        "duplicate_cadence_slots": {str(stamp): count for stamp, count in duplicates.items() if count > 1},
        "model_forecast_or_trade_coverage": None, "profitability_validated": False,
        "limitations": ["Closed verified prefix only; active tail is excluded and may contain later records",
            "Cadence expectation does not prove an undiscovered market was listed by the venue",
            "Different markets sharing one cadence slot are flagged, not assumed independent",
            "Unverified-clock observations use a ±10 second local-time candidate band, not a verified UTC decision window",
            "Coverage concerns frozen input features, not fitted model probabilities or executable orders"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--feature-version',choices=(VERSION,CAUSAL_VERSION),default=VERSION)
    args = parser.parse_args()
    frozen, invalid_files = {}, []
    for path in (args.data / ('decisions-v3' if args.feature_version==CAUSAL_VERSION else 'decisions')).glob("event-*.json"):
        try:
            snapshot, digest = read_decision(path)
            if snapshot.get("session_id") == args.session and snapshot["feature_version"] == args.feature_version:
                frozen[snapshot["market"]["market_id"]] = (snapshot, digest)
        except (ValueError, KeyError, TypeError, OSError):
            invalid_files.append(path.name)
    result = audit(session_records(args.data, args.session), frozen, args.session,feature_version=args.feature_version)
    result["invalid_decision_files"] = invalid_files
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    print(json.dumps({key:result[key] for key in ("announced_market_count", "announced_finished_window_count", "valid_frozen_input_count",
        "input_coverage_among_announced_finished_windows", "status_counts", "expected_cadence_slot_count", "expected_slots_not_announced")}))
