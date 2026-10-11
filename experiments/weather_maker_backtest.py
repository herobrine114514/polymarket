"""Would resting quotes around a fair value have been filled, and how did the fills end? Offline.

At each decision time a buy and a sell quote for the Yes token are placed at fair value
minus/plus a margin, never through the public midpoint. A quote counts as filled only if
later public taker trades went through at a strictly worse price for the taker than the
quote (so the quote would have had price priority) for at least the quote size, within the
resting window. Fills are held to the official resolution; makers pay no fee on these
markets and rebates are ignored.

Taker trades are mapped to the Yes token: selling Yes at p or buying No at 1-p hits a Yes
bid; buying Yes at p or selling No at 1-p lifts a Yes offer.

What this cannot show: how other makers would have reacted to the quote, queue position
at an equal price (never counted as a fill), and anything about orders larger than the
taker flow seen. It replays public history; it is not an execution record.
Run in .polycast/oss-numeric-venv. Nothing here places or routes orders.
"""
from __future__ import annotations
import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.btc5m_backfill import read_rows

TICK = .01
VARIANTS = ("mid", "sharpened", "model", "stack")
PRIMARY = {"lead": 2, "variant": "stack", "margin": .05, "window_hours": 12}   # fixed before looking at fills


def quote_prices(fair: float, mid: float, margin: float) -> tuple[float | None, float | None]:
    """Bid and offer on the tick grid: `margin` away from fair value and at least a tick from the midpoint."""
    bid = math.floor(min(fair - margin, mid - TICK) / TICK + 1e-9) * TICK
    offer = math.ceil(max(fair + margin, mid + TICK) / TICK - 1e-9) * TICK
    return (round(bid, 2) if bid >= TICK else None), (round(offer, 2) if offer <= 1 - TICK else None)


def filled(trades: list[list[float]], start: int, end: int, bid: float | None, offer: float | None,
           size: float) -> tuple[bool, bool]:
    """Whether taker flow strictly through each quote reached `size` shares in (start, end]."""
    sold = bought = 0.0
    for timestamp, outcome, taker_buys, price, shares, *_ in trades:
        if not start < timestamp <= end:
            continue
        yes_price = price if outcome == 0 else 1 - price
        hits_bid = (outcome == 0) != bool(taker_buys)        # sell Yes, or buy No
        if hits_bid and bid is not None and yes_price < bid - 1e-9:
            sold += shares
        if not hits_bid and offer is not None and yes_price > offer + 1e-9:
            bought += shares
    return sold >= size, bought >= size


def cluster_ci(values: np.ndarray, clusters: np.ndarray, rng: np.random.Generator, draws: int) -> list[float]:
    _, inverse = np.unique(clusters, return_inverse=True)
    count = inverse.max() + 1
    total, weight = np.bincount(inverse, weights=values), np.bincount(inverse).astype(float)
    picks = rng.integers(0, count, size=(draws, count))
    return [float(value) for value in np.percentile(total[picks].sum(axis=1) / np.maximum(weight[picks].sum(axis=1), 1), [2.5, 97.5])]


def tape_benchmark(quotes: list[dict], trades: dict[str, list], hours: int, draws: int) -> dict:
    """Score the fair values against what takers actually paid shortly before the decision.

    Where spreads are wide the midpoint is a poor statement of the market's view; the
    volume-weighted Yes-equivalent price of trades in the `hours` before the decision is a
    better one. Events are scored only if at least half the probability mass has such trades;
    buckets without any fall back to the midpoint. Distributions are normalized per event.
    """
    by_event: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in quotes:
        by_event[(row["event"], row["lead"])].append(row)
    rng = np.random.default_rng(20261012)
    result = {}
    for lead in sorted({key[1] for key in by_event}):
        scores, days = defaultdict(list), []
        for (event, event_lead), rows in by_event.items():
            if event_lead != lead or not all(row["id"] in trades for row in rows):
                continue
            taped, covered = [], 0.0
            for row in rows:
                recent = [(price if outcome == 0 else 1 - price, shares) for timestamp, outcome, _, price, shares, *_
                          in trades[row["id"]] if row["at"] - hours * 3600 < timestamp <= row["at"]]
                volume = sum(shares for _, shares in recent)
                if volume > 0:
                    covered += row["market"]
                    taped.append(sum(price * shares for price, shares in recent) / volume)
                else:
                    taped.append(row["mid"])
            if covered < .5:
                continue
            y = np.asarray([row["y"] for row in rows])
            forecasts = {"tape": np.clip(np.asarray(taped), 1e-3, 1), **{key: np.asarray([row[key] for row in rows])
                                                                         for key in ("market", "sharpened", "model", "stack")}}
            for key, values in forecasts.items():
                values = values / values.sum()
                scores[key + "_log_loss"].append(-float(np.log(max(1e-4, float(values[y == 1][0])))))
                scores[key + "_rps"].append(float(np.sum((np.cumsum(values) - np.cumsum(y)) ** 2)))
            days.append(rows[0]["date"])
        if len(days) < 40:
            result[f"lead{lead}"] = {"events": len(days), "skipped": "too few events with recent trades"}
            continue
        table = {key: np.asarray(values) for key, values in scores.items()}
        day_ids = np.asarray(days)
        entry = {"events": len(days), "hours_before_decision": hours,
                 "scores": {key: float(values.mean()) for key, values in table.items()}, "versus_tape": {}}
        for key in ("market", "sharpened", "model", "stack"):
            entry["versus_tape"][key] = {
                metric: {"difference": float((table[f"{key}_{metric}"] - table[f"tape_{metric}"]).mean()),
                         "ci95_by_day": cluster_ci(table[f"{key}_{metric}"] - table[f"tape_{metric}"], day_ids, rng, draws)}
                for metric in ("log_loss", "rps")}
        result[f"lead{lead}"] = entry
    return result


def replay(quotes: list[dict], trades: dict[str, list], margins: list[float], windows: list[int], size: float,
           delay: int, draws: int) -> dict:
    by_event: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in quotes:
        by_event[(row["event"], row["lead"])].append(row)
    usable = [row for rows in by_event.values() if all(item["id"] in trades for item in rows) for row in rows]
    rng = np.random.default_rng(20261011)
    report = {"quote_rows": len(quotes), "quote_rows_with_trades": len(usable),
              "events_with_trades": len({(row["event"], row["lead"]) for row in usable}), "size_shares": size,
              "primary": PRIMARY, "results": []}
    for lead in sorted({row["lead"] for row in usable}):
        part = [row for row in usable if row["lead"] == lead]
        for variant in VARIANTS:
            for margin in margins:
                for hours in windows:
                    pnl, days, sides, posted = [], [], [], 0
                    for row in part:
                        bid, offer = quote_prices(row[variant], row["mid"], margin)
                        posted += (bid is not None) + (offer is not None)
                        got_bid, got_offer = filled(trades[row["id"]], row["at"] + delay, row["at"] + hours * 3600, bid, offer, size)
                        if got_bid:
                            pnl.append(row["y"] - bid)
                            days.append(row["date"])
                            sides.append("bid")
                        if got_offer:
                            pnl.append(offer - row["y"])
                            days.append(row["date"])
                            sides.append("offer")
                    entry = {"lead": lead, "variant": variant, "margin": margin, "window_hours": hours,
                             "events": len({row["event"] for row in part}), "quotes_posted": posted, "fills": len(pnl)}
                    if len(pnl) >= 30:
                        values, day_ids, side_ids = np.asarray(pnl), np.asarray(days), np.asarray(sides)
                        by_day = np.asarray([values[day_ids == day].sum() for day in np.unique(day_ids)])
                        entry.update({
                            "fill_rate": len(pnl) / max(posted, 1), "mean_pnl_per_share": float(values.mean()),
                            "ci95_by_day": cluster_ci(values, day_ids, rng, draws),
                            "pnl_per_quote_posted": float(values.sum() / max(posted, 1)),
                            "total_pnl_usd_at_size": float(values.sum() * size), "days": int(len(by_day)),
                            "positive_days": int((by_day > 0).sum()), "worst_day_usd": float(by_day.min() * size),
                            "by_side": {side: {"fills": int((side_ids == side).sum()),
                                               "mean_pnl_per_share": float(values[side_ids == side].mean())}
                                        for side in ("bid", "offer") if (side_ids == side).any()}})
                    report["results"].append(entry)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".polycast/slow-market-backfill-v1"))
    parser.add_argument("--quotes", type=Path, default=Path(".polycast/slow-market-backfill-v1/weather-quotes.jsonl"))
    parser.add_argument("--margins", type=float, nargs="*", default=[.03, .05, .08])
    parser.add_argument("--windows", type=int, nargs="*", default=[2, 6, 12, 24], help="hours a quote rests")
    parser.add_argument("--size", type=float, default=10, help="shares per quote")
    parser.add_argument("--delay", type=int, default=120, help="seconds after the decision before a quote can fill")
    parser.add_argument("--draws", type=int, default=2000)
    parser.add_argument("--tape-hours", type=int, default=6, help="hours of trades before the decision used as the market's view")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    quotes = read_rows(args.quotes)
    earliest: dict[str, int] = {}
    for row in quotes:
        earliest[row["id"]] = min(earliest.get(row["id"], row["at"]), row["at"])
    trades = {}
    with (args.directory / "trades.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row["id"] in earliest:
                trades[row["id"]] = [trade for trade in row["trades"]
                                     if trade[0] > earliest[row["id"]] - args.tape_hours * 3600]
    report = replay(quotes, trades, args.margins, args.windows, args.size, args.delay, args.draws)
    report["tape_benchmark"] = tape_benchmark(quotes, trades, args.tape_hours, args.draws)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"quote rows={report['quote_rows']} with trades={report['quote_rows_with_trades']} "
          f"event-decisions={report['events_with_trades']} size={args.size:g} shares")
    for lead, entry in report["tape_benchmark"].items():
        if "skipped" in entry:
            print(f"tape benchmark {lead}: {entry}")
            continue
        print(f"tape benchmark {lead}: events={entry['events']} (trades in the {entry['hours_before_decision']}h before the decision) "
              + " ".join(f"{key}={value:.4f}" for key, value in entry["scores"].items() if key.endswith("log_loss")))
        for key, item in entry["versus_tape"].items():
            print(f"    {key:9s} minus tape: " + " ".join(
                f"{metric}={value['difference']:+.4f}[{value['ci95_by_day'][0]:+.4f},{value['ci95_by_day'][1]:+.4f}]"
                for metric, value in item.items()))
    for entry in report["results"]:
        if "mean_pnl_per_share" not in entry:
            continue
        primary = all(entry[key] == value for key, value in PRIMARY.items())
        sides = " ".join(f"{side}:{item['fills']}@{item['mean_pnl_per_share']:+.3f}" for side, item in entry["by_side"].items())
        print(f"{'*' if primary else ' '} lead{entry['lead']} {entry['variant']:9s} m={entry['margin']:.2f} w={entry['window_hours']:2d}h "
              f"fills={entry['fills']:5d} ({entry['fill_rate']:.1%}) pnl/share={entry['mean_pnl_per_share']:+.4f} "
              f"ci_day=[{entry['ci95_by_day'][0]:+.4f},{entry['ci95_by_day'][1]:+.4f}] total=${entry['total_pnl_usd_at_size']:+.0f} "
              f"days+={entry['positive_days']}/{entry['days']} worst=${entry['worst_day_usd']:+.0f} | {sides}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
