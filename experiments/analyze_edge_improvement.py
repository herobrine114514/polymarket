"""Read-only handover audit. Historical pooling/filtering is exploratory only.

Scores immutable, reviewed, causally available forecasts against the same captured
two-sided market book. No retrospective recalculation earns prospective credit.
The original script and report remain in the handover source snapshot.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from polycast.analysis import apply_bayesian_shrinkage
from polycast.market import parse_utc
from experiments.btc5m_model_diagnostics import strict_mid

HANDOVER_BOUNDARY = datetime(2026, 10, 3, 13, 40, tzinfo=timezone.utc)


def instant(value):
    return parse_utc(value) if isinstance(value, str) else None


def probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def qualify(row, conn, cutoff):
    """Return one immutable observation or a fixed exclusion reason."""
    try:
        output, payload, draft = (json.loads(row[k] or "null") for k in
                                  ("model_output_json", "payload_json", "result_json"))
        if not all(isinstance(v, dict) for v in (output, payload, draft)):
            return None, "missing_frozen_forecast"
        if row["draft_count"] != 1 or row["event_markets"] != 1:
            return None, "ambiguous_frozen_forecast_or_event"
        as_of, reviewed, end = (instant(v) for v in (row["as_of"], output.get("reviewed_at"), row["end_at"]))
        created, resolved, recorded = (instant(row[k]) for k in ("draft_created_at", "resolved_at", "recorded_at"))
        if not all((as_of, reviewed, end, created, resolved, recorded)):
            return None, "missing_or_pending_timestamps"
        if not as_of <= created <= reviewed < end <= resolved <= recorded <= cutoff:
            return None, "late_or_inconsistent_timestamps"
        if (row["winning_side"] not in ("YES", "NO") or
                urlparse(row["source_url"] or "").hostname != "gamma-api.polymarket.com"):
            return None, "unverified_resolution_source"
        for frozen in (payload, draft, output):
            if frozen.get("market_id") != row["market_id"] or instant(frozen.get("as_of")) != as_of:
                return None, "forecast_identity_mismatch"
        if payload.get("category") != "crypto5m" or instant(payload.get("end_at")) != end:
            return None, "market_rules_mismatch"
        review = output.get("review")
        if not isinstance(review, dict) or review.get("approved") is not True:
            return None, "review_rejected_or_missing"
        if (row["abstain"] != 0 or output.get("abstain") is not False or
                draft.get("abstain") is not False or output.get("analysis_abstain") is True):
            return None, "abstention"
        effective, raw = output.get("p_yes"), output.get("raw_p_yes", output.get("p_yes"))
        if (not all(probability(v) for v in (row["probability"], effective, raw)) or
                row["probability"] != effective or draft.get("p_yes") != effective or
                draft.get("raw_p_yes", draft.get("p_yes")) != raw):
            return None, "stored_probability_mismatch"
        evidence = payload.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return None, "missing_evidence"
        ids = set()
        for item in evidence:
            if (not isinstance(item, dict) or type(item.get("id")) is not int or
                    item["id"] <= 0 or item["id"] in ids):
                return None, "ambiguous_evidence"
            ids.add(item["id"])
            record = conn.execute("SELECT fetched_at FROM evidence WHERE id=?", (item["id"],)).fetchone()
            times = [instant(item.get(k)) for k in ("published_at", "first_seen_at", "source_updated_at")]
            times.append(instant(record["fetched_at"]) if record else None)
            if not all(times) or max(times) > as_of:
                return None, "evidence_unavailable_at_as_of"
        for field in ("supporting_evidence_ids", "counter_evidence_ids"):
            refs = draft.get(field)
            if not isinstance(refs, list) or any(type(i) is not int or i not in ids for i in refs):
                return None, "invalid_evidence_reference"
        market = strict_mid(row, as_of)
        if market is None:
            return None, "no_identical_fresh_two_sided_prior"
        model, prompt = output.get("model_version"), output.get("prompt_version")
        if not all(isinstance(v, str) and v for v in (model, prompt)):
            return None, "unknown_model_version"
        era = "after_handover_boundary" if as_of >= HANDOVER_BOUNDARY else "before_handover_boundary"
        if prompt.startswith("btc5m-v7-"):
            era = "audit_repair_v7"
        return {"prediction_id": row["id"], "event_id": row["event_id"], "market_id": row["market_id"],
                "as_of": as_of.isoformat(), "reviewed_at": reviewed.isoformat(), "end_at": end.isoformat(),
                "model_version": model, "prompt_version": prompt, "era": era,
                "review_method": review.get("review_method", "legacy_model_review"),
                "raw": float(raw), "effective": float(effective), "market": market,
                "outcome": int(row["winning_side"] == "YES"),
                "draft_sha256": hashlib.sha256(row["result_json"].encode()).hexdigest(),
                "payload_sha256": hashlib.sha256(row["payload_json"].encode()).hexdigest()}, None
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return None, "malformed_record"


def metrics(events, field):
    if not events:
        return {"events": 0, "brier": None, "log_loss": None, "directional_accuracy": None}
    ps = [e[field] for e in events]
    directional = [e for e in events if e[field] != .5]
    return {"events": len(events),
            "brier": sum((p-e["outcome"])**2 for p,e in zip(ps,events)) / len(events),
            "log_loss": -sum(e["outcome"]*math.log(max(1e-12,p)) +
                             (1-e["outcome"])*math.log(max(1e-12,1-p)) for p,e in zip(ps,events)) / len(events),
            "directional_events": len(directional),
            "directional_accuracy": sum((e[field]>.5)==bool(e["outcome"]) for e in directional) / len(directional)
                                     if directional else None}


def run_analysis(db_path: Path, cutoff=None):
    cutoff = cutoff or datetime.now(timezone.utc)
    with closing(sqlite3.connect(db_path.resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        rows = [dict(r) for r in conn.execute("""SELECT p.*,m.event_id,m.end_at,
            r.winning_side,r.resolved_at,r.recorded_at,r.source_url,
            (SELECT COUNT(*) FROM markets s WHERE s.event_id=m.event_id) AS event_markets,
            (SELECT COUNT(*) FROM analysis_drafts x WHERE x.market_id=p.market_id AND x.as_of=p.as_of
                AND x.rules_hash=p.rules_hash AND x.evidence_hash=p.evidence_hash) AS draft_count,
            d.payload_json,d.result_json,d.created_at AS draft_created_at,
            b.captured_at,b.book_timestamp,b.p_yes_mid AS book_mid,
            b.yes_best_bid AS book_bid,b.yes_best_ask AS book_ask,
            a.captured_at AS staged_at,a.book_timestamp AS staged_book_at,
            a.yes_best_bid AS staged_bid,a.yes_best_ask AS staged_ask
            FROM predictions p JOIN markets m ON m.id=p.market_id
            LEFT JOIN resolutions r ON r.market_id=p.market_id
            LEFT JOIN analysis_drafts d ON d.market_id=p.market_id AND d.as_of=p.as_of
                AND d.rules_hash=p.rules_hash AND d.evidence_hash=p.evidence_hash
            LEFT JOIN market_baselines b ON b.prediction_id=p.id
            LEFT JOIN analysis_market_baselines a ON a.market_id=p.market_id AND a.as_of=p.as_of
                AND a.rules_hash=p.rules_hash AND a.evidence_hash=p.evidence_hash
            WHERE m.category='crypto5m'""")]
        rows.sort(key=lambda r: (instant(r["as_of"]) or datetime.min.replace(tzinfo=timezone.utc),r["id"]),reverse=True)
        exclusions, chosen, seen = Counter(), [], set()
        for row in rows:
            # One latest stored prediction per event; a rejected latest result cannot
            # make a favorable older forecast eligible again.
            if row["event_id"] in seen:
                exclusions["older_prediction_for_same_event"] += 1
                continue
            seen.add(row["event_id"])
            item, reason = qualify(row,conn,cutoff)
            if reason:
                exclusions[reason] += 1
            else:
                chosen.append(item)
        groups = defaultdict(list)
        for item in chosen:
            groups[(item["era"],item["model_version"],item["prompt_version"])].append(item)
        cohorts = [{"era":key[0], "model":key[1], "prompt":key[2],
                    "scores":{field:metrics(items,field) for field in ("raw","effective","market")},
                    "independent_events":len(items)} for key,items in sorted(groups.items())]
        exploration = []
        for weight in (.1,.15,.2,.25,.3):
            items = [{**e,"counterfactual":apply_bayesian_shrinkage(e["raw"],e["market"],lambda_shrink=weight)} for e in chosen]
            exploration.append({"lambda":weight,"scores":metrics(items,"counterfactual")})
        orders = [dict(r) for r in conn.execute("""SELECT o.*,r.winning_side,r.recorded_at
            FROM paper_orders o JOIN markets m ON m.id=o.market_id
            JOIN resolutions r ON r.market_id=o.market_id
            WHERE m.category='crypto5m' AND o.status='settled'""")]
        settled = [r for r in orders if instant(r['recorded_at']) and instant(r['recorded_at'])<=cutoff
                   and r['winning_side'] in ('YES','NO') and r['settlement'] in (0.,1.)
                   and (r['settlement']==1.)==(r['side']==r['winning_side'])]
        def pnl(items):
            cost = sum((Decimal(str(r['total_cost'])) for r in items),Decimal(0))
            profit = sum((Decimal(str(r['shares']))*Decimal(str(r['settlement']))-Decimal(str(r['total_cost'])) for r in items),Decimal(0))
            return {"orders":len(items),"wins":sum(r['settlement']==1. for r in items),
                    "cost_usd":str(cost),"pnl_after_recorded_execution_fees_usd":str(profit),
                    "model_cost_deducted":False,"live_execution_proven":False}
        costs = [dict(r) for r in conn.execute("""SELECT r.id,u.cost_lower_usd,u.cost_upper_usd
            FROM model_runs r LEFT JOIN model_usage u ON u.call_id=r.id
            WHERE r.started_at>=? AND r.started_at<=?""",(HANDOVER_BOUNDARY.isoformat(),cutoff.isoformat()))]
        priced = [r for r in costs if r['cost_lower_usd'] is not None and r['cost_upper_usd'] is not None]
        cost_report = {"scope":"All recorded model calls after handover boundary; not attributed per order",
                       "calls":len(costs),"priced_calls":len(priced),"complete":bool(costs) and len(costs)==len(priced),
                       "lower_usd":str(sum((Decimal(r['cost_lower_usd']) for r in priced),Decimal(0))),
                       "upper_usd":str(sum((Decimal(r['cost_upper_usd']) for r in priced),Decimal(0)))}
    return {"generated_at":cutoff.isoformat(),"mode":"read-only-paper-audit","live_ready":False,
            "handover_boundary":HANDOVER_BOUNDARY.isoformat(),
            "boundary_is_complete_source_registration":False,
            "limitations":["V6 version tag was reused across source changes; time boundary is descriptive only",
                           "Pooling sweep and historical price-band selection are exploratory counterfactuals",
                           "Small observational cohorts do not prove predictive advantage or net profitability",
                           "Deterministic review validates envelopes, not model reasoning or calibration"],
            "input_btc5m_rows":len(rows),"independent_event_candidates":len(seen),
            "exclusions":dict(exclusions),"strict_cohorts":cohorts,
            "strict_events":sorted(chosen,key=lambda e:e['as_of']),
            "exploration_no_prospective_credit":{"log_odds_pooling_sweep":exploration,
                    "actual_historical_paper_orders":pnl(settled),
                    "retrospective_price_band_subset":pnl([r for r in settled if .15<=r['avg_price']<=.85])},
            "recorded_model_costs_after_boundary":cost_report,"external_orders_submitted":0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database",type=Path,default=ROOT/".polycast/polycast.sqlite3")
    parser.add_argument("--output",type=Path,default=ROOT/".polycast/handover-code-audit-20261003/evaluation-fixed.json")
    args = parser.parse_args()
    report = run_analysis(args.database)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.output.exists():
        raise SystemExit("Refusing to overwrite a fixed audit observation; choose a new --output")
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps({k:report[k] for k in ('generated_at','input_btc5m_rows','exclusions','strict_cohorts',
                                          'recorded_model_costs_after_boundary','live_ready')},ensure_ascii=False))
