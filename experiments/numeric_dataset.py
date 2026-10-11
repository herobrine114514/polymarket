"""Join immutable microstructure decisions to existing verified official results.

Read-only SQLite, no network calls, no training and no rewriting old forecasts.
The label observation clock is retained for chronological fitting.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.numeric_models import feature_vector
from experiments.causal_features import VERSION as CAUSAL_VERSION
from polycast.market import parse_utc


def read_decision(path: Path) -> tuple[dict, str]:
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    raw = wrapper["snapshot_json"]
    if (type(wrapper.get("schema_version")) is not int or wrapper["schema_version"] != 1 or
            wrapper.get("decision_protocol") not in ("start-plus-120s-tolerance-3s-v1", "utc-start-plus-120s-tolerance-3s-v2",'utc-start-plus-120s-tolerance-3s-v3') or
            not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != wrapper.get("snapshot_sha256")):
        raise ValueError("Frozen snapshot hash/protocol mismatch")
    snapshot = json.loads(raw)
    if snapshot.get("feature_version") not in ("btc5m-public-microstructure-v1", "btc5m-public-microstructure-v2-clock-checked",CAUSAL_VERSION) or snapshot.get("kind") != "feature_snapshot":
        raise ValueError("Unknown feature version")
    protocols={'btc5m-public-microstructure-v1':'start-plus-120s-tolerance-3s-v1',
      'btc5m-public-microstructure-v2-clock-checked':'utc-start-plus-120s-tolerance-3s-v2',CAUSAL_VERSION:'utc-start-plus-120s-tolerance-3s-v3'}
    if protocols[snapshot['feature_version']]!=wrapper['decision_protocol']:
        raise ValueError("Decision clock protocol/version mismatch")
    return snapshot, wrapper["snapshot_sha256"]


def assemble(directory: Path, conn: sqlite3.Connection, cutoff_ms: int, *, feature_version: str | None = None, capture_hashes: dict | None = None) -> dict:
    allowed = ("btc5m-public-microstructure-v1", "btc5m-public-microstructure-v2-clock-checked",CAUSAL_VERSION)
    if feature_version is not None and feature_version not in allowed:
        raise ValueError("Unknown requested feature version")
    if capture_hashes is not None and (not isinstance(capture_hashes,dict) or not capture_hashes or
        any(not isinstance(k,str) or not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for k,v in capture_hashes.items())):
        raise ValueError('Invalid requested capture hashes')
    outcomes = {row["market_id"]: dict(row) for row in conn.execute("""SELECT
        m.id AS market_id,m.event_id,m.description,m.end_at,m.yes_token,m.no_token,
        r.winning_side,r.source_url,r.resolved_at,r.recorded_at,
        s.status AS check_status,s.official_side
        FROM resolutions r JOIN markets m ON m.id=r.market_id
        LEFT JOIN settlement_checks s ON s.market_id=m.id WHERE m.category='crypto5m'""")}
    events, exclusions, observed_ids, versions = [], Counter(), set(), set()
    coverage = {"frozen_decisions": 0, "complete_features": 0, "pending_or_missing_labels": 0}
    candidates = []
    folders=['decisions-v3'] if feature_version==CAUSAL_VERSION else ['decisions'] if feature_version else ['decisions','decisions-v3']
    for path in sorted(p for folder in folders for p in (directory/folder).glob('event-*.json')):
        coverage["frozen_decisions"] += 1
        try:
            snapshot, digest = read_decision(path)
            if feature_version is not None and snapshot["feature_version"] != feature_version:
                exclusions["different_feature_version"] += 1
                continue
            if capture_hashes is not None and snapshot.get('source_hashes')!=capture_hashes:
                exclusions['different_capture_source_hashes']+=1
                continue
            decision = snapshot.get("as_of_utc_ms") if snapshot["feature_version"]!='btc5m-public-microstructure-v1' else snapshot["received_at_ms"]
            if type(decision) is not int or decision > cutoff_ms:
                exclusions["future_decision"] += 1
                continue
            features = feature_vector(snapshot)
            candidates.append((snapshot, digest, decision, features))
        except (ValueError, KeyError, TypeError, OSError):
            exclusions["incomplete_or_invalid_frozen_features"] += 1

    from experiments.journal_replay import verify_causal_batch
    causal = [(snapshot, digest) for snapshot, digest, _, _ in candidates if snapshot['feature_version'] == CAUSAL_VERSION]
    audit = verify_causal_batch(directory, causal)
    causal_results = iter(audit['results'])
    journal_rejections = Counter()
    for snapshot, digest, decision, features in candidates:
        try:
            if snapshot['feature_version'] == CAUSAL_VERSION:
                verification = next(causal_results)
                if verification['status'] != 'verified_causal_context':
                    journal_rejections[verification['error']] += 1
                    raise ValueError(verification['error'])
            versions.add(snapshot["feature_version"])
            coverage["complete_features"] += 1
            market = snapshot["market"]
            event_id = market["event_id"]
            if event_id in observed_ids:
                raise ValueError("Duplicate event decision")
            observed_ids.add(event_id)
            label = outcomes.get(market["market_id"])
            if label is None:
                coverage["pending_or_missing_labels"] += 1
                continue
            resolved, recorded, end = (parse_utc(label[name]) for name in ("resolved_at", "recorded_at", "end_at"))
            if (not all((resolved, recorded, end)) or label["check_status"] != "verified" or
                    label["official_side"] != label["winning_side"] or label["winning_side"] not in ("YES", "NO") or
                    not label["source_url"].startswith("https://gamma-api.polymarket.com/events/slug/btc-updown-5m-") or
                    label["event_id"] != event_id or label["description"] != market["rules"] or
                    label["yes_token"] != market["up_asset"] or label["no_token"] != market["down_asset"] or
                    int(end.timestamp() * 1000) != market["end_at_ms"] or
                    int(recorded.timestamp() * 1000) < market["end_at_ms"] or
                    int(resolved.timestamp() * 1000) < market["end_at_ms"]):
                exclusions["official_label_or_contract_not_verified"] += 1
                continue
            observed = max(int(recorded.timestamp() * 1000), int(resolved.timestamp() * 1000))
            if observed > cutoff_ms:
                coverage["pending_or_missing_labels"] += 1
                continue
            events.append({"event_id": event_id, "market_id": market["market_id"],
                           "as_of_ms": decision, "end_at_ms": market["end_at_ms"], "feature_version": snapshot["feature_version"],
                           "label_observed_at_ms": observed, "outcome": int(label["winning_side"] == "YES"),
                           "features": features, "market_probability": snapshot["polymarket"]["up"]["midpoint"],
                           "capture_source_hashes": snapshot.get("source_hashes", {}),
                           "snapshot_sha256": digest, "source_url": label["source_url"],
                           "independent_reference_verified": False})
        except (ValueError, KeyError, TypeError, OSError):
            exclusions["incomplete_or_invalid_frozen_features"] += 1
    if len(versions) > 1:
        raise ValueError("Mixed capture/clock versions require explicit feature-version selection")
    return {"schema_version": 1, "dataset_version": 'btc5m-causal-labels-v3' if feature_version==CAUSAL_VERSION else "btc5m-public-microstructure-labels-v2",
            "requested_feature_version": feature_version, "feature_versions": sorted(versions),
            'requested_capture_hashes':capture_hashes,
            "cutoff_ms": cutoff_ms, "events": sorted(events, key=lambda row: (row["as_of_ms"], row["event_id"])),
            "coverage": coverage, "exclusions": dict(exclusions), "profitability_validated": False,
            "journal_audit": audit['diagnostics'], "journal_rejections": dict(journal_rejections),
            "limitation": "Coverage is among frozen decisions; all discovered/missed events need a separate denominator audit."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--feature-version", choices=("btc5m-public-microstructure-v1", "btc5m-public-microstructure-v2-clock-checked",CAUSAL_VERSION),
                        default="btc5m-public-microstructure-v2-clock-checked")
    parser.add_argument('--capture-hashes',type=Path,help='JSON object of explicitly registered capture source hashes')
    args = parser.parse_args()
    conn = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = assemble(args.data, conn, int(datetime.now(timezone.utc).timestamp() * 1000), feature_version=args.feature_version,
          capture_hashes=json.loads(args.capture_hashes.read_text(encoding='utf-8')) if args.capture_hashes else None)
    finally:
        conn.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"events": len(result["events"]), "coverage": result["coverage"], "exclusions": result["exclusions"]}))
