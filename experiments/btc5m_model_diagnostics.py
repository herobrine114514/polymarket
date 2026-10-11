"""Read-only BTC 5m comparisons using probabilities frozen before each outcome.

No model, network, order, or database write is performed. This is a diagnostic
of engineering samples, not a frozen strategy's out-of-sample validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polycast.market import parse_utc

VERSION = "btc5m-frozen-diagnostics-v1"
PATTERNS = {
    "relay": ("chainlink_btc_twap60", re.compile(
        r"illustrative same-feed diffusion proxy P\(Up\) (0\.\d{4}|1\.0000) over"), 30),
    "spot": ("coinbase_btc_5m", re.compile(
        r"spot-diffusion proxy P\(Up\) (0\.\d{4}|1\.0000)\."), 90),
}


def probability(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and
            math.isfinite(value) and 0 <= value <= 1)


def frozen_proxy(payload: dict, kind: str, as_of: datetime) -> float | None:
    source, pattern, max_age = PATTERNS[kind]
    values = []
    for evidence in payload.get("evidence", []):
        if not isinstance(evidence, dict) or evidence.get("source_id") != source:
            continue
        published = parse_utc(evidence.get("published_at"))
        first_seen = parse_utc(evidence.get("first_seen_at"))
        if (not published or not first_seen or not published <= first_seen <= as_of or
                not 0 <= (as_of - published).total_seconds() <= max_age):
            continue
        summary = evidence.get("summary")
        match = pattern.search(summary) if isinstance(summary, str) else None
        if match:
            values.append(float(match.group(1)))
    # Ambiguous multiple snapshots are missing, never cherry-picked by outcome.
    return values[0] if len(values) == 1 else None


def strict_mid(row: dict, as_of: datetime) -> float | None:
    fields = ("book_bid", "book_ask", "staged_bid", "staged_ask", "book_mid")
    if not all(probability(row[k]) and 0 < row[k] < 1 for k in fields):
        return None
    captured = parse_utc(row["captured_at"])
    book_at = parse_utc(row["book_timestamp"])
    if (row["book_bid"] > row["book_ask"] or
            row["book_bid"] != row["staged_bid"] or row["book_ask"] != row["staged_ask"] or
            not math.isclose(row["book_mid"], (row["book_bid"] + row["book_ask"]) / 2,
                             rel_tol=0, abs_tol=1e-9) or
            not captured or not book_at or parse_utc(row["staged_at"]) != captured or
            parse_utc(row["staged_book_at"]) != book_at or
            not 0 <= (as_of - captured).total_seconds() <= 10 or
            not 0 <= (captured - book_at).total_seconds() <= 10):
        return None
    return float(row["book_mid"])


def scores(events: list[dict], fields: tuple[str, ...]) -> dict:
    paired = [e for e in events if not e["analysis_abstain"] and
              all(e.get(field) is not None for field in fields)]
    metrics = {}
    for field in fields:
        bins = []
        for index in range(5):
            bucket = [e for e in paired if min(4, int(e[field] * 5)) == index]
            bins.append({"lower": index / 5, "upper": (index + 1) / 5,
                         "events": len(bucket),
                         "mean_probability": round(sum(e[field] for e in bucket) / len(bucket), 6)
                         if bucket else None,
                         "observed_up_fraction": round(sum(e["outcome"] for e in bucket) / len(bucket), 6)
                         if bucket else None})
        directional = [e for e in paired if e[field] != .5]
        metrics[field] = {
            "brier": round(sum((e[field] - e["outcome"]) ** 2 for e in paired) / len(paired), 6)
            if paired else None,
            "directional_events": len(directional),
            "directional_correct": sum((e[field] > .5) == bool(e["outcome"]) for e in directional),
            "ties_at_half": len(paired) - len(directional), "calibration_bins": bins,
        }
    differences = {}
    for field in fields:
        if field != "model":
            differences[field] = round(sum((e["model"] - e["outcome"]) ** 2 -
                                          (e[field] - e["outcome"]) ** 2 for e in paired) /
                                       len(paired), 6) if paired else None
    return {"events": len(paired), "utc_dates": sorted({e["as_of"][:10] for e in paired}),
            "metrics": metrics, "model_minus_baseline_brier": differences}


def build_report(conn: sqlite3.Connection, cutoff: datetime) -> dict:
    cutoff = cutoff.astimezone(timezone.utc)
    rows = [dict(r) for r in conn.execute("""SELECT p.*,m.event_id,m.end_at,
        r.winning_side,r.resolved_at,r.recorded_at,
        (SELECT COUNT(*) FROM markets s WHERE s.event_id=m.event_id) AS event_markets,
        d.payload_json,d.result_json,
        b.captured_at,b.book_timestamp,b.p_yes_mid AS book_mid,
        b.yes_best_bid AS book_bid,b.yes_best_ask AS book_ask,
        a.captured_at AS staged_at,a.book_timestamp AS staged_book_at,
        a.yes_best_bid AS staged_bid,a.yes_best_ask AS staged_ask
        FROM predictions p JOIN markets m ON m.id=p.market_id
        JOIN resolutions r ON r.market_id=p.market_id
        LEFT JOIN analysis_drafts d ON d.market_id=p.market_id AND d.as_of=p.as_of
          AND d.rules_hash=p.rules_hash AND d.evidence_hash=p.evidence_hash
        LEFT JOIN market_baselines b ON b.prediction_id=p.id
        LEFT JOIN analysis_market_baselines a ON a.market_id=p.market_id AND a.as_of=p.as_of
          AND a.rules_hash=p.rules_hash AND a.evidence_hash=p.evidence_hash
        WHERE m.category='crypto5m'""")]
    rows.sort(key=lambda r: (parse_utc(r["as_of"]) or datetime.min.replace(tzinfo=timezone.utc),
                             r["id"]), reverse=True)
    chosen, exclusions = {}, Counter()
    for row in rows:
        if row["event_id"] in chosen:
            exclusions["older_forecast_for_selected_event"] += 1
            continue
        if row["event_markets"] != 1:
            exclusions["unexpected_multi_market"] += 1
            continue
        try:
            output = json.loads(row["model_output_json"])
            payload = json.loads(row["payload_json"] or "null")
            draft = json.loads(row["result_json"] or "null")
            as_of, end = parse_utc(row["as_of"]), parse_utc(row["end_at"])
            reviewed = parse_utc(output.get("reviewed_at"))
            resolved, recorded = parse_utc(row["resolved_at"]), parse_utc(row["recorded_at"])
            if (not all((as_of, end, reviewed, resolved, recorded)) or
                    not as_of <= reviewed < min(end, resolved) or
                    resolved > cutoff or recorded > cutoff):
                exclusions["late_or_unknown_timestamps"] += 1
                continue
            if (not isinstance(payload, dict) or not isinstance(draft, dict) or
                    payload.get("market_id") != row["market_id"] or
                    payload.get("category") != "crypto5m" or
                    parse_utc(payload.get("as_of")) != as_of or parse_utc(payload.get("end_at")) != end or
                    draft.get("market_id") != row["market_id"] or parse_utc(draft.get("as_of")) != as_of or
                    not probability(output.get("p_yes")) or draft.get("p_yes") != output["p_yes"] or
                    not isinstance(draft.get("abstain"), bool) or
                    ("analysis_abstain" in output and output["analysis_abstain"] is not draft["abstain"]) or
                    not isinstance(output.get("review", {}).get("approved"), bool)):
                exclusions["missing_or_inconsistent_frozen_analysis"] += 1
                continue
            model, prompt = output.get("model_version"), output.get("prompt_version")
            approved = output["review"]["approved"]
            chosen[row["event_id"]] = {
                "event_id": row["event_id"], "market_id": row["market_id"], "prediction_id": row["id"],
                "as_of": as_of.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "reviewed_at": reviewed.astimezone(timezone.utc).isoformat(),
                "end_at": end.astimezone(timezone.utc).isoformat(),
                "resolved_at": resolved.astimezone(timezone.utc).isoformat(),
                "recorded_at": recorded.astimezone(timezone.utc).isoformat(), "winning_side": row["winning_side"],
                "outcome": int(row["winning_side"] == "YES"),
                "model_version": model if isinstance(model, str) and model else "unknown",
                "prompt_version": prompt if isinstance(prompt, str) and prompt else "unknown",
                "analysis_abstain": draft["abstain"], "review_approved": approved,
                "model": float(output["p_yes"]), "market": strict_mid(row, as_of),
                "relay": frozen_proxy(payload, "relay", as_of), "spot": frozen_proxy(payload, "spot", as_of),
                "payload_sha256": hashlib.sha256(row["payload_json"].encode()).hexdigest(),
            }
        except (ValueError, TypeError, KeyError, AttributeError):
            exclusions["malformed_record"] += 1
    events = sorted(chosen.values(), key=lambda e: (e["as_of"], e["prediction_id"]))

    def cohort(items: list[dict]) -> dict:
        return {"selected_events": len(items),
                "original_abstentions": sum(e["analysis_abstain"] for e in items),
                "market_pair": scores(items, ("model", "market")),
                "relay_pair": scores(items, ("model", "relay")),
                "common_market_relay": scores(items, ("model", "market", "relay")),
                "common_all_sources": scores(items, ("model", "market", "relay", "spot"))}

    models = sorted({e["model_version"] for e in events})
    by_version = {model: {prompt: {
        "all_raw": cohort([e for e in events if e["model_version"] == model and e["prompt_version"] == prompt]),
        "approved": cohort([e for e in events if e["model_version"] == model and e["prompt_version"] == prompt and e["review_approved"]]),
        "rejected": cohort([e for e in events if e["model_version"] == model and e["prompt_version"] == prompt and not e["review_approved"]]),
    } for prompt in sorted({e["prompt_version"] for e in events if e["model_version"] == model})}
                  for model in models}
    encoded = json.dumps(events, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"version": VERSION, "cutoff_utc": cutoff.isoformat(timespec="seconds"),
            "selection": "Latest eligible completed original analysis per settled single-market BTC 5m event, before both window end and first result; choose before proxy availability; original abstentions retained in coverage but not scored",
            "limitations": ["Engineering diagnostic, not frozen out-of-sample strategy evidence",
                            "Approved and rejected subsets are selected; neither is trading win rate",
                            "Adjacent windows and days can be dependent; no IID confidence interval or pass threshold",
                            "RTDS and Coinbase probabilities are uncalibrated proxies included in model input, not independent signed settlement data",
                            "Market mid is a benchmark probability, not an executable order price"],
            "data_sha256": hashlib.sha256(encoded).hexdigest(), "exclusions": dict(exclusions),
            "overall": cohort(events), "by_model_and_prompt": by_version, "events": events}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(".polycast/polycast.sqlite3"))
    parser.add_argument("--cutoff", help="Explicit UTC cutoff; future results excluded")
    parser.add_argument("--output", type=Path, default=Path(".polycast/btc5m-diagnostics.json"))
    args = parser.parse_args()
    cutoff = parse_utc(args.cutoff) if args.cutoff else datetime.now(timezone.utc)
    if cutoff is None:
        parser.error("--cutoff must be a timezone-aware timestamp")
    # WAL readers obtain a consistent snapshot without invoking schema migrations.
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        result = build_report(conn, cutoff)
    result["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    comparison = result["overall"]["common_market_relay"]
    print(json.dumps({"output": str(args.output.resolve()), "cutoff": result["cutoff_utc"],
                      "data_sha256": result["data_sha256"],
                      "selected_events": result["overall"]["selected_events"],
                      "common_market_relay_events": comparison["events"],
                      "common_brier": {k:v["brier"] for k,v in comparison["metrics"].items()},
                      "exclusions": result["exclusions"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
