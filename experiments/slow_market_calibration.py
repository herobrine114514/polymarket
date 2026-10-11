"""Are slow Polymarket markets miscalibrated, and does it survive costs? Offline research.

One row per (market, horizon): the public price of the first outcome token at a time fixed
in advance relative to the scheduled game start or end date, kept only if the market had
opened and had not yet closed. Outcomes are the official Gamma resolutions. Uncertainty is
bootstrapped over whole events, because markets of one event share an outcome.
Recalibration is fitted on the earlier part of the sample and scored on the later part.
Costs: public taker fee rate*p*(1-p) plus an assumed half spread; fills are an assumption.
Run in .polycast/oss-numeric-venv. Nothing here places or routes orders.
"""
from __future__ import annotations
import argparse
import bisect
import json
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.btc5m_backfill import read_rows

SPORT_HORIZONS = {"24h": 86400, "6h": 21600, "1h": 3600, "10m": 600}
OTHER_HORIZONS = {"7d": 604800, "3d": 259200, "1d": 86400, "6h": 21600, "1h": 3600}
FAST_CRYPTO = re.compile(r"-updown-|up-or-down")


def category(market: dict) -> str:
    fee_type = market.get("fee_type") or "none"
    if fee_type.startswith("sports"):
        return "sports:" + (market.get("sports_type") or "other")
    if fee_type.startswith("crypto"):
        return "crypto:updown" if FAST_CRYPTO.search(market.get("slug") or "") else "crypto:other"
    return fee_type.replace("_fees", "")


def sample_rows(markets: list[dict], prices: dict[str, list], max_gap: int) -> list[dict]:
    rows = []
    for market in markets:
        history = prices.get(market["id"])
        if not history or market["outcome_prices"] not in (["1", "0"], ["0", "1"]):
            continue
        history = sorted(history)
        times = [point[0] for point in history]
        sport = market.get("game_start") is not None
        anchor = market["game_start"] if sport else market["end"]
        if anchor is None:
            continue
        for label, seconds in (SPORT_HORIZONS if sport else OTHER_HORIZONS).items():
            at = anchor - seconds
            if market.get("start") and at < market["start"] + 3600:
                continue                                  # not open long enough to have a settled price
            if market.get("closed_at") and at >= market["closed_at"] - 60:
                continue                                  # already resolved at the sampling time
            position = bisect.bisect_right(times, at) - 1
            if position < 0 or at - times[position] > max_gap:
                continue
            price = history[position][1]
            if not 0 < price < 1:
                continue
            rows.append({"id": market["id"], "event": market.get("event_id") or market["id"],
                         "category": category(market), "series": market.get("series"), "horizon": label, "at": at,
                         "price": price, "y": 1.0 if market["outcome_prices"] == ["1", "0"] else 0.0,
                         "fee_rate": market.get("fee_rate") or 0.0, "volume": market.get("volume") or 0.0,
                         "yes_no": market["outcomes"] == ["Yes", "No"]})
    return rows


def fit_calibration(price: np.ndarray, y: np.ndarray, weight: np.ndarray | None = None) -> tuple[float, float]:
    """y ~ sigmoid(a + b*logit(price)); (0, 1) means the price is calibrated."""
    x = logit(np.clip(price, 1e-3, 1 - 1e-3))
    w = np.ones(len(y)) if weight is None else weight

    def objective(params):
        margin = params[0] + params[1] * x
        residual = w * (expit(margin) - y)
        return (w * (np.logaddexp(0, margin) - y * margin)).sum() / w.sum(), np.asarray([residual.sum(), (residual * x).sum()]) / w.sum()

    return tuple(minimize(objective, np.asarray([0.0, 1.0]), jac=True, method="L-BFGS-B").x)


def cluster_weights(clusters: np.ndarray, rng: np.random.Generator, draws: int):
    _, inverse = np.unique(clusters, return_inverse=True)
    count = inverse.max() + 1
    for _ in range(draws):
        yield np.bincount(rng.integers(0, count, count), minlength=count)[inverse].astype(float)


def describe(rows: list[dict], rng: np.random.Generator, draws: int) -> dict:
    price = np.asarray([row["price"] for row in rows])
    y = np.asarray([row["y"] for row in rows])
    events = np.asarray([row["event"] for row in rows])
    keep = (price >= .02) & (price <= .98)
    price, y, events = price[keep], y[keep], events[keep]
    if len(y) < 200 or len(np.unique(events)) < 60 or len(np.unique(y)) < 2:
        return {"rows": int(len(y)), "events": int(len(np.unique(events))), "skipped": "too few rows or events"}
    intercept, slope = fit_calibration(price, y)
    fits = np.asarray([fit_calibration(price, y, weight) for weight in cluster_weights(events, rng, draws)])
    favourite, won = np.maximum(price, 1 - price), np.where(price >= .5, y, 1 - y)
    bins = []
    for low, high in ((.5, .6), (.6, .7), (.7, .8), (.8, .9), (.9, .981)):
        mask = (favourite >= low) & (favourite < high)
        if mask.sum() >= 30:
            gaps = [(weight[mask] * (won[mask] - favourite[mask])).sum() / max(weight[mask].sum(), 1)
                    for weight in cluster_weights(events, rng, min(draws, 300))]
            bins.append({"favourite_price": [low, high], "rows": int(mask.sum()),
                         "mean_price": float(favourite[mask].mean()), "win_rate": float(won[mask].mean()),
                         "gap": float(won[mask].mean() - favourite[mask].mean()),
                         "gap_ci95": [float(value) for value in np.percentile(gaps, [2.5, 97.5])]})
    return {"rows": int(len(y)), "events": int(len(np.unique(events))),
            "brier_price": float(np.mean((price - y) ** 2)), "first_outcome_rate": float(y.mean()),
            "mean_price": float(price.mean()), "intercept": float(intercept), "slope": float(slope),
            "intercept_ci95": [float(value) for value in np.percentile(fits[:, 0], [2.5, 97.5])],
            "slope_ci95": [float(value) for value in np.percentile(fits[:, 1], [2.5, 97.5])],
            "favourite_bins": bins}


def out_of_sample(rows: list[dict], rng: np.random.Generator, draws: int, half_spread: float) -> dict:
    """Fit on the earliest 60% of sampling times, then score and paper-trade the rest."""
    rows = sorted((row for row in rows if .02 <= row["price"] <= .98), key=lambda row: row["at"])
    cut = rows[int(len(rows) * .6)]["at"] if rows else 0
    fit_rows, test_rows = [row for row in rows if row["at"] < cut], [row for row in rows if row["at"] >= cut]
    fit_events = {row["event"] for row in fit_rows}
    test_rows = [row for row in test_rows if row["event"] not in fit_events]   # an event never straddles the split
    if len(fit_rows) < 300 or len(test_rows) < 200 or len({row["y"] for row in fit_rows}) < 2:
        return {"skipped": "too few rows"}
    intercept, slope = fit_calibration(np.asarray([row["price"] for row in fit_rows]), np.asarray([row["y"] for row in fit_rows]))
    price = np.asarray([row["price"] for row in test_rows])
    y = np.asarray([row["y"] for row in test_rows])
    events = np.asarray([row["event"] for row in test_rows])
    model = expit(intercept + slope * logit(np.clip(price, 1e-3, 1 - 1e-3)))
    difference = (model - y) ** 2 - (price - y) ** 2
    samples = [(weight * difference).sum() / weight.sum() for weight in cluster_weights(events, rng, draws)]
    result = {"fit_rows": len(fit_rows), "test_rows": len(test_rows), "test_events": int(len(np.unique(events))),
              "intercept": float(intercept), "slope": float(slope), "brier_price": float(np.mean((price - y) ** 2)),
              "brier_recalibrated": float(np.mean((model - y) ** 2)), "brier_difference": float(difference.mean()),
              "brier_difference_ci95": [float(value) for value in np.percentile(samples, [2.5, 97.5])], "trading": []}
    fee_rate = np.asarray([row["fee_rate"] for row in test_rows])
    for threshold in (0., .01, .02, .03):
        pnl, pnl_events, seen = [], [], set()
        for index, row in enumerate(test_rows):
            best = None
            for p_side, mid, win in ((model[index], price[index], y[index]), (1 - model[index], 1 - price[index], 1 - y[index])):
                cost = mid + half_spread
                cost += fee_rate[index] * cost * (1 - cost)
                if .03 <= mid <= .97 and p_side - cost >= threshold and (best is None or p_side - cost > best[0]):
                    best = (p_side - cost, win - cost)
            if best and row["id"] not in seen:          # one entry per market, at its earliest qualifying horizon
                seen.add(row["id"])
                pnl.append(best[1])
                pnl_events.append(row["event"])
        if len(pnl) >= 30:
            values = np.asarray(pnl)
            means = [(weight * values).sum() / max(weight.sum(), 1) for weight in cluster_weights(np.asarray(pnl_events), rng, draws)]
            result["trading"].append({"threshold": threshold, "half_spread": half_spread, "entries": len(values),
                                      "events": len(set(pnl_events)), "mean_pnl_per_share": float(values.mean()),
                                      "ci95": [float(value) for value in np.percentile(means, [2.5, 97.5])],
                                      "win_rate": float(np.mean(values > 0))})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".polycast/slow-market-backfill-v1"))
    parser.add_argument("--fidelity", type=int, default=60)
    parser.add_argument("--draws", type=int, default=600)
    parser.add_argument("--half-spread", type=float, default=.01)
    parser.add_argument("--complete-only", action="store_true",
                        help="only events whose every market is on file (complete-events.jsonl)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    markets = read_rows(args.directory / "markets.jsonl")
    if args.complete_only:
        complete = {row["slug"] for row in read_rows(args.directory / "complete-events.jsonl")}
        markets = [market for market in markets if market.get("event_slug") in complete]
    prices = {row["id"]: row["history"] for row in read_rows(args.directory / f"prices-f{args.fidelity}.jsonl")}
    rows = sample_rows(markets, prices, max_gap=max(3 * args.fidelity * 60, 7200))
    rng = np.random.default_rng(20261009)
    report = {"markets_with_prices": len(prices), "rows": len(rows), "groups": {}}
    groups = Counter((row["category"], row["horizon"]) for row in rows)
    for (name, horizon), count in sorted(groups.items()):
        subset = [row for row in rows if row["category"] == name and row["horizon"] == horizon]
        entry = describe(subset, rng, args.draws)
        if "skipped" not in entry:
            entry["out_of_sample"] = out_of_sample(subset, rng, args.draws, args.half_spread)
        report["groups"][f"{name}@{horizon}"] = entry
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"markets_with_prices={report['markets_with_prices']} rows={report['rows']}")
    for key, entry in report["groups"].items():
        if "skipped" in entry:
            continue
        test = entry["out_of_sample"]
        line = (f"{key:34s} n={entry['rows']:5d} ev={entry['events']:5d} brier={entry['brier_price']:.4f} "
                f"a={entry['intercept']:+.3f} [{entry['intercept_ci95'][0]:+.2f},{entry['intercept_ci95'][1]:+.2f}] "
                f"b={entry['slope']:.3f} [{entry['slope_ci95'][0]:.2f},{entry['slope_ci95'][1]:.2f}]")
        if "skipped" not in test:
            line += (f" | oos d={test['brier_difference']:+.5f} [{test['brier_difference_ci95'][0]:+.5f},"
                     f"{test['brier_difference_ci95'][1]:+.5f}]")
            for trade in test["trading"]:
                if trade["threshold"] == .02:
                    line += f" trade@2c n={trade['entries']} pnl={trade['mean_pnl_per_share']:+.4f} [{trade['ci95'][0]:+.3f},{trade['ci95'][1]:+.3f}]"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
