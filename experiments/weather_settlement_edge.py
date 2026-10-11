"""Do weather prices lag the day's own observations? Offline replay of public trades.

Dead buckets: once a METAR for the market's local day reports a temperature above a
bucket's range, that bucket can no longer win. Later trades that sold Yes exposure in it
(sold Yes, or bought No) at a Yes price above zero collected a near-certain gain; this
measures how much of that happened, how long after the observation, and how often the
"dead" call was wrong against the official resolution.

Standing leader: at fixed local hours the bucket holding the day's maximum so far is
bought at the price the next real taker paid for Yes exposure, and held to resolution.

An observation counts as known `--delay` seconds after its time stamp. Taker fee is
rate*c*(1-c) on the token bought. Replays public history; not an execution record.
Run in .polycast/oss-numeric-venv. Nothing here places or routes orders.
"""
from __future__ import annotations
import argparse
import bisect
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.btc5m_backfill import read_rows
from experiments.weather_edge import shift_date, weather_events
from experiments.weather_maker_backtest import cluster_ci

DELAY_BINS = ((0, 60), (60, 300), (300, 900), (900, 3600), (3600, 10 ** 9))


def reported(celsius: float, unit: str) -> int:
    return round(celsius if unit == "C" else celsius * 1.8 + 32)


def day_window(station: dict, date: str) -> tuple[int, int] | None:
    """UTC epochs of the local day's start and end, from the station's own local/epoch time grid."""
    lookup = station.setdefault("_epoch_of", dict(zip(station["local_time"], station["epoch"])))
    start, end = lookup.get(f"{date}T00:00"), lookup.get(f"{shift_date(date, 1)}T00:00")
    return (start, end) if start is not None and end is not None else None


def yes_flow(trade: list) -> tuple[bool, float]:
    """(sells Yes exposure, Yes-equivalent price) for one taker trade."""
    _, outcome, taker_buys, price, *_ = trade
    return (outcome == 0) != bool(taker_buys), price if outcome == 0 else 1 - price


def dead_buckets(events: dict, stations: dict, observations: dict, trades: dict, delay: int, minimum_price: float,
                 margin: int) -> dict:
    rows, calls, wrong = [], 0, 0
    for event in events.values():
        station, window = stations.get(event["station"]), None
        if station is not None:
            window = day_window(station, event["date"])
        series = observations.get(event["station"])
        if window is None or not series:
            continue
        first = bisect.bisect_left(series, [window[0], -1e9])
        last = bisect.bisect_left(series, [window[1], -1e9])
        running, known = -10 ** 9, []                      # (time known, running maximum in reported units)
        for epoch, celsius in series[first:last]:
            value = reported(celsius, event["unit"])
            if value > running:
                running = value
                known.append((epoch + delay, value))
        for bucket in event["buckets"]:
            # `margin` whole degrees above the range guards against rounding and later-corrected readings.
            died = next((at for at, value in known if value >= bucket["high"] + margin), None)
            if died is None or bucket["id"] not in trades:
                continue
            calls += 1
            wrong += bucket["yes"]
            for trade in trades[bucket["id"]]:
                if trade[0] <= died:
                    continue
                sells, price = yes_flow(trade)
                if price < minimum_price:
                    continue
                cost = 1 - price if sells else price       # token bought: No when selling Yes exposure
                fee = bucket["fee_rate"] * cost * (1 - cost)
                gain = (price - fee if sells else -price - fee) + (0 if not bucket["yes"] else (-1 if sells else 1))
                rows.append({"date": event["date"], "delay": trade[0] - died, "sells": sells, "price": price,
                             "shares": trade[4], "gain": gain})
    result = {"buckets_declared_dead": calls, "declared_dead_but_won": int(wrong), "minimum_yes_price": minimum_price,
              "margin_degrees": margin,
              "by_delay": []}
    days = len({row["date"] for row in rows}) or 1
    for low, high in DELAY_BINS:
        part = [row for row in rows if low <= row["delay"] < high and row["sells"]]
        buys = [row for row in rows if low <= row["delay"] < high and not row["sells"]]
        shares = sum(row["shares"] for row in part)
        result["by_delay"].append({
            "seconds_after_known": [low, min(high, 10 ** 6)], "sell_trades": len(part), "sell_shares": shares,
            "mean_yes_price": sum(row["price"] * row["shares"] for row in part) / shares if shares else None,
            "taker_gain_usd": sum(row["gain"] * row["shares"] for row in part),
            "taker_gain_usd_per_day": sum(row["gain"] * row["shares"] for row in part) / days,
            "buy_trades_into_dead_bucket": len(buys), "buy_shares": sum(row["shares"] for row in buys)})
    return result


def standing_leader(events: dict, stations: dict, observations: dict, trades: dict, delay: int, hours: list[int],
                    wait: int, draws: int) -> list[dict]:
    rng = np.random.default_rng(20261013)
    result = []
    for hour in hours:
        pnl, days, wins, prices, margins = [], [], [], [], []
        for event in events.values():
            station = stations.get(event["station"])
            window = day_window(station, event["date"]) if station is not None else None
            series = observations.get(event["station"])
            if window is None or not series:
                continue
            decision = window[0] + hour * 3600
            first = bisect.bisect_left(series, [window[0], -1e9])
            last = bisect.bisect_right(series, [decision - delay, 1e9])
            if last - first < max(6, hour // 2):
                continue                                   # too few observations to trust the maximum so far
            day = series[first:last]
            top = max(reported(celsius, event["unit"]) for _, celsius in day)
            leader = next((bucket for bucket in event["buckets"] if bucket["low"] <= top < bucket["high"]), None)
            if leader is None or leader["id"] not in trades:
                continue
            paid = next((yes_flow(trade)[1] for trade in trades[leader["id"]]
                         if decision < trade[0] <= decision + wait and not yes_flow(trade)[0]), None)
            if paid is None or not .02 <= paid <= .98:
                continue
            pnl.append(float(leader["yes"]) - paid - leader["fee_rate"] * paid * (1 - paid))
            days.append(event["date"])
            wins.append(float(leader["yes"]))
            prices.append(paid)
            margins.append(max(celsius for _, celsius in day) - day[-1][1])
        if len(pnl) < 30:
            result.append({"local_hour": hour, "entries": len(pnl), "skipped": "too few entries"})
            continue
        values, cooled = np.asarray(pnl), np.asarray(margins) >= 2
        entry = {"local_hour": hour, "entries": len(values), "leader_win_rate": float(np.mean(wins)),
                 "mean_price_paid": float(np.mean(prices)), "mean_pnl_per_share": float(values.mean()),
                 "ci95_by_day": cluster_ci(values, np.asarray(days), rng, draws)}
        if cooled.sum() >= 30:                              # already 2 C below the day's maximum
            entry["cooled_2c"] = {"entries": int(cooled.sum()), "leader_win_rate": float(np.asarray(wins)[cooled].mean()),
                                  "mean_price_paid": float(np.asarray(prices)[cooled].mean()),
                                  "mean_pnl_per_share": float(values[cooled].mean()),
                                  "ci95_by_day": cluster_ci(values[cooled], np.asarray(days)[cooled], rng, draws)}
        result.append(entry)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".polycast/slow-market-backfill-v1"))
    parser.add_argument("--trades", type=Path, default=Path(".polycast/slow-market-backfill-v1/trades.jsonl"))
    parser.add_argument("--delay", type=int, default=600, help="seconds until an observation counts as known")
    parser.add_argument("--minimum-price", type=float, default=.02, help="ignore dead-bucket trades below this Yes price")
    parser.add_argument("--margin", type=int, default=1, help="whole degrees above a bucket before it is called dead")
    parser.add_argument("--hours", type=int, nargs="*", default=[15, 16, 17, 18, 19, 20, 21, 22])
    parser.add_argument("--wait", type=int, default=1800, help="seconds to wait for the next real buy of the leader")
    parser.add_argument("--draws", type=int, default=2000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    weather = args.directory / "weather"
    events = weather_events(read_rows(args.directory / "markets.jsonl"))
    complete = {row["slug"] for row in read_rows(args.directory / "complete-events.jsonl")}
    events = {slug: event for slug, event in events.items() if slug in complete}
    stations = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in weather.glob("????.json")}
    observations = {path.stem[4:]: json.loads(path.read_text(encoding="utf-8")) for path in weather.glob("obs-????.json")}
    wanted = {bucket["id"] for event in events.values() for bucket in event["buckets"]}
    trades = {}
    with args.trades.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row["id"] in wanted:
                trades[row["id"]] = row["trades"]
    report = {"events": len(events), "markets_with_trades": len(trades), "delay_seconds": args.delay,
              "dead_buckets": dead_buckets(events, stations, observations, trades, args.delay, args.minimum_price, args.margin),
              "standing_leader": standing_leader(events, stations, observations, trades, args.delay, args.hours,
                                                 args.wait, args.draws)}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    dead = report["dead_buckets"]
    print(f"events={report['events']} markets with trades={report['markets_with_trades']} delay={args.delay}s")
    print(f"dead buckets: declared={dead['buckets_declared_dead']} of which won anyway={dead['declared_dead_but_won']}")
    for item in dead["by_delay"]:
        price = "-" if item["mean_yes_price"] is None else f"{item['mean_yes_price']:.3f}"
        print(f"  {item['seconds_after_known'][0]:>5d}s+ sells={item['sell_trades']:5d} shares={item['sell_shares']:9.0f} "
              f"mean Yes price={price} taker gain=${item['taker_gain_usd']:8.0f} (${item['taker_gain_usd_per_day']:.1f}/day) "
              f"| buys into dead bucket={item['buy_trades_into_dead_bucket']} shares={item['buy_shares']:.0f}")
    for item in report["standing_leader"]:
        if "skipped" in item:
            print(f"leader at {item['local_hour']:02d}h: {item['entries']} entries (skipped)")
            continue
        line = (f"leader at {item['local_hour']:02d}h: n={item['entries']:4d} wins={item['leader_win_rate']:.3f} "
                f"paid={item['mean_price_paid']:.3f} pnl/share={item['mean_pnl_per_share']:+.4f} "
                f"[{item['ci95_by_day'][0]:+.4f},{item['ci95_by_day'][1]:+.4f}]")
        if "cooled_2c" in item:
            cool = item["cooled_2c"]
            line += (f" | cooled 2C: n={cool['entries']} wins={cool['leader_win_rate']:.3f} paid={cool['mean_price_paid']:.3f} "
                     f"pnl={cool['mean_pnl_per_share']:+.4f} [{cool['ci95_by_day'][0]:+.4f},{cool['ci95_by_day'][1]:+.4f}]")
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
