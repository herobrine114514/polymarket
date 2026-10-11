"""Do some wallets trade weather markets persistently well, and can their buys be followed? Offline.

Rating. Each public taker trade priced between 0.15 and 0.85 is scored against the official
resolution before fees (buy: payoff - price; sell: price - payoff). Per wallet and event the
share-weighted sum is divided by its standard deviation under "the price was fair"
(sqrt(sum shares^2 * p * (1-p))), giving one roughly unit-variance score per event; trades
outside that band are ignored because collecting small premiums on near-certain outcomes
looks like skill until the rare loss arrives. A wallet's statistic on a UTC day is the sum
of its event scores over sqrt(count), using only events dated at least two days earlier
(resolved by then). "Sharp": at least `--minimum-events` such events and a statistic of 2
or more; "dull": -2 or less. By chance alone about 2.5% of rated wallets land in each.

Following. When a rated wallet buys at least `--minimum-shares` of a token inside the band,
the follower buys the same token at the price the next real taker buy of it printed,
between `--delay` and `--wait` seconds later, pays the taker fee and holds to resolution,
once per token per event. The "same price" figure assumes the leader's own price was still
available and is an upper bound. Wallet ids are truncated public proxy-wallet addresses.
Replays public history; not an execution record. Run in .polycast/oss-numeric-venv.
"""
from __future__ import annotations
import argparse
import bisect
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.btc5m_backfill import read_rows
from experiments.weather_edge import shift_date, weather_events
from experiments.weather_maker_backtest import cluster_ci
from experiments.weather_settlement_edge import day_window

BAND = (.15, .85)


def utc_date(epoch: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def trade_result(trade: list, yes: bool) -> tuple[float, float]:
    """(profit per share before fees if held to resolution, token price) for one taker trade."""
    _, outcome, taker_buys, price, *_ = trade
    payoff = float(yes) if outcome == 0 else float(not yes)
    return (payoff - price if taker_buys else price - payoff), price


def event_scores(per_event: dict[str, dict[str, list[float]]]) -> dict[str, tuple[list[str], np.ndarray]]:
    """wallet -> (event dates, standardized score per event)."""
    return {wallet: (sorted(events), np.asarray([events[date][0] / max(events[date][1], 1e-12) ** .5 for date in sorted(events)]))
            for wallet, events in per_event.items()}


class Ratings:
    """Wallet statistics as of a UTC day, never reading events dated within the two days before it."""

    def __init__(self, scores: dict[str, tuple[list[str], np.ndarray]]):
        self.table = {wallet: (dates, np.cumsum(values)) for wallet, (dates, values) in scores.items()}
        self.cache: dict[tuple[str, str], tuple[int, float]] = {}

    def as_of(self, wallet: str, day: str) -> tuple[int, float]:
        key = (wallet, day)
        if key not in self.cache:
            record = self.table.get(wallet)
            count = bisect.bisect_right(record[0], shift_date(day, -2)) if record else 0
            self.cache[key] = (count, float(record[1][count - 1] / count ** .5) if count else 0.0)
        return self.cache[key]

    def kind(self, wallet: str, day: str, minimum_events: int) -> str | None:
        count, statistic = self.as_of(wallet, day)
        if count < minimum_events:
            return None
        return "sharp" if statistic >= 2 else "dull" if statistic <= -2 else "other"


def follow(events: dict, trades: dict, starts: dict, ratings: Ratings, minimum_events: int, minimum_shares: float,
           delay: int, wait: int, draws: int) -> dict:
    rng = np.random.default_rng(20261014)
    entries = defaultdict(list)       # kind -> (date, follower profit, same-price profit, before the day, slippage)
    for event in events.values():
        for bucket in event["buckets"]:
            tape = trades.get(bucket["id"]) or []
            taken: set[tuple[str, int]] = set()
            for index, trade in enumerate(tape):
                timestamp, outcome, taker_buys, price, shares, wallet = trade
                if not taker_buys or shares < minimum_shares or not BAND[0] <= price <= BAND[1] or not wallet:
                    continue
                kind = ratings.kind(wallet, utc_date(timestamp), minimum_events)
                if kind is None or (kind, outcome) in taken:
                    continue
                later = next((other for other in tape[index + 1:] if other[1] == outcome and other[2] and
                              timestamp + delay < other[0] <= timestamp + wait), None)
                if later is None:
                    continue
                taken.add((kind, outcome))
                payoff = float(bucket["yes"]) if outcome == 0 else float(not bucket["yes"])
                paid, rate = later[3], bucket["fee_rate"]
                entries[kind].append((event["date"], payoff - paid - rate * paid * (1 - paid),
                                      payoff - price - rate * price * (1 - price),
                                      timestamp < starts.get(event["slug"], 0), paid - price))
    report = {}
    for kind, rows in entries.items():
        days = np.asarray([row[0] for row in rows])
        followed, same = np.asarray([row[1] for row in rows]), np.asarray([row[2] for row in rows])
        early = np.asarray([row[3] for row in rows])
        entry = {"entries": len(rows), "days": int(len(np.unique(days))),
                 "mean_pnl_per_share": float(followed.mean()), "ci95_by_day": cluster_ci(followed, days, rng, draws),
                 "same_price_upper_bound": float(same.mean()), "same_price_ci95_by_day": cluster_ci(same, days, rng, draws),
                 "mean_price_slippage": float(np.mean([row[4] for row in rows]))}
        for label, mask in (("before_the_day", early), ("on_the_day", ~early)):
            if mask.sum() >= 30:
                entry[label] = {"entries": int(mask.sum()), "mean_pnl_per_share": float(followed[mask].mean()),
                                "ci95_by_day": cluster_ci(followed[mask], days[mask], rng, draws),
                                "same_price_upper_bound": float(same[mask].mean())}
        report[kind] = entry
    return report


def persistence(scores: dict[str, tuple[list[str], np.ndarray]], minimum_events: int) -> dict:
    """Does a wallet's standardized record in the first half of the dates carry into the second half?"""
    dates = sorted({date for wallet_dates, _ in scores.values() for date in wallet_dates})
    split = dates[len(dates) // 2]
    first, second, groups = [], [], defaultdict(list)
    for wallet_dates, values in scores.values():
        cut = bisect.bisect_left(wallet_dates, split)
        if cut < minimum_events or len(values) - cut < minimum_events:
            continue
        early = float(values[:cut].sum() / cut ** .5)
        late = float(values[cut:].sum() / (len(values) - cut) ** .5)
        first.append(early)
        second.append(late)
        groups["sharp" if early >= 2 else "dull" if early <= -2 else "other"].append(late)
    result = {"split_date": split, "wallets_in_both_halves": len(first),
              "correlation_of_statistics": float(np.corrcoef(first, second)[0, 1]) if len(first) > 10 else None}
    for kind in ("sharp", "other", "dull"):
        later = np.asarray(groups[kind])
        result[f"first_half_{kind}"] = {"wallets": int(len(later)),
                                        "mean_later_statistic": float(later.mean()) if len(later) else None,
                                        "share_later_positive": float(np.mean(later > 0)) if len(later) else None,
                                        "share_later_sharp": float(np.mean(later >= 2)) if len(later) else None}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".polycast/slow-market-backfill-v1"))
    parser.add_argument("--minimum-events", type=int, default=8)
    parser.add_argument("--minimum-shares", type=float, default=10)
    parser.add_argument("--delay", type=int, default=60)
    parser.add_argument("--wait", type=int, default=1800)
    parser.add_argument("--draws", type=int, default=2000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    events = weather_events(read_rows(args.directory / "markets.jsonl"))
    fetched = {row["slug"] for row in read_rows(args.directory / "trades-events-done.jsonl")}
    events = {slug: event for slug, event in events.items() if slug in fetched}
    stations = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in (args.directory / "weather").glob("????.json")}
    starts = {}
    for slug, event in events.items():
        window = day_window(stations[event["station"]], event["date"]) if event["station"] in stations else None
        if window:
            starts[slug] = window[0]
    wanted = {bucket["id"]: (slug, bucket) for slug, event in events.items() for bucket in event["buckets"]}
    trades, per_event, takers = {}, defaultdict(dict), set()
    profit_usd = value_usd = fees_usd = 0.0
    with (args.directory / "trades.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row["id"] not in wanted:
                continue
            slug, bucket = wanted[row["id"]]
            trades[row["id"]] = row["trades"]
            date = events[slug]["date"]
            for trade in row["trades"]:
                gross, price = trade_result(trade, bucket["yes"])
                shares, wallet = trade[4], trade[5]
                profit_usd += gross * shares
                value_usd += price * shares
                fees_usd += bucket["fee_rate"] * price * (1 - price) * shares
                takers.add(wallet)
                if BAND[0] <= price <= BAND[1]:
                    record = per_event[wallet].setdefault(date, [0.0, 0.0])
                    record[0] += gross * shares
                    record[1] += shares ** 2 * price * (1 - price)
    scores = event_scores(per_event)
    ratings = Ratings(scores)
    last_day = max(event["date"] for event in events.values())
    kinds = defaultdict(int)
    for wallet in takers:
        kinds[ratings.kind(wallet, shift_date(last_day, 3), args.minimum_events) or "too_few_events"] += 1
    report = {"events": len(events), "takers": len(takers), "wallet_kinds_at_end": dict(kinds),
              "all_takers": {"gross_profit_usd": profit_usd, "fees_usd": fees_usd, "traded_value_usd": value_usd,
                             "net_return_on_value": (profit_usd - fees_usd) / value_usd},
              "persistence": persistence(scores, args.minimum_events),
              "following": follow(events, trades, starts, ratings, args.minimum_events, args.minimum_shares,
                                  args.delay, args.wait, args.draws)}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    everyone = report["all_takers"]
    print(f"events={report['events']} takers={report['takers']} kinds at end={report['wallet_kinds_at_end']}")
    print(f"all takers: gross ${everyone['gross_profit_usd']:,.0f} fees ${everyone['fees_usd']:,.0f} on ${everyone['traded_value_usd']:,.0f} "
          f"traded = {everyone['net_return_on_value']:+.2%} net")
    print("persistence:", json.dumps(report["persistence"]))
    for kind in ("sharp", "other", "dull"):
        entry = report["following"].get(kind)
        if not entry:
            continue
        line = (f"follow {kind:5s}: n={entry['entries']:5d} days={entry['days']} pnl/share={entry['mean_pnl_per_share']:+.4f} "
                f"[{entry['ci95_by_day'][0]:+.4f},{entry['ci95_by_day'][1]:+.4f}] same-price={entry['same_price_upper_bound']:+.4f} "
                f"[{entry['same_price_ci95_by_day'][0]:+.4f},{entry['same_price_ci95_by_day'][1]:+.4f}] slippage={entry['mean_price_slippage']:+.4f}")
        for label in ("before_the_day", "on_the_day"):
            if label in entry:
                item = entry[label]
                line += (f" | {label}: n={item['entries']} {item['mean_pnl_per_share']:+.4f} "
                         f"[{item['ci95_by_day'][0]:+.4f},{item['ci95_by_day'][1]:+.4f}] same-price={item['same_price_upper_bound']:+.4f}")
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
