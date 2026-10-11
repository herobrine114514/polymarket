"""Backfill resolved Polymarket markets and their price history for calibration research.

Unauthenticated public GETs only (Gamma market listing, CLOB prices-history, Data API trades). Slow markets
move little within the ~15 s by which public price history trails the live book, so that
history is an acceptable price record here; it was not for BTC 5m. No orders, no account
access, no production imports. Resumable: finished items are skipped on rerun.
"""
from __future__ import annotations
import argparse
import calendar
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.btc5m_backfill import CLOB, GAMMA, append, fetch_json, read_rows

DATA = "https://data-api.polymarket.com"


def stamp(second: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(second))


def parse(value: str | None) -> int | None:
    """Gamma mixes `2026-10-07T00:00:00Z` and `2026-10-07 03:04:26+00`; both are UTC."""
    if not value:
        return None
    text = value.replace("T", " ").replace("Z", "").split("+")[0].split(".")[0].strip()
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def market_row(market: dict) -> dict | None:
    try:
        tokens, outcomes = json.loads(market["clobTokenIds"]), json.loads(market["outcomes"])
        prices = json.loads(market.get("outcomePrices") or "[]")
    except (KeyError, TypeError, ValueError):
        return None
    if len(tokens) != 2 or len(outcomes) != 2:
        return None
    event = (market.get("events") or [{}])[0]
    fee = market.get("feeSchedule") or {}
    return {"id": str(market["id"]), "question": market.get("question"), "slug": market.get("slug"),
            "condition_id": market.get("conditionId"), "tokens": tokens, "outcomes": outcomes,
            "outcome_prices": prices, "volume": market.get("volumeNum"),
            "start": parse(market.get("startDate")), "end": parse(market.get("endDate")),
            "closed_at": parse(market.get("closedTime")), "game_start": parse(market.get("gameStartTime")),
            "neg_risk": bool(market.get("negRisk")), "fee_type": market.get("feeType"),
            "fee_rate": fee.get("rate"), "fee_exponent": fee.get("exponent"),
            "sports_type": market.get("sportsMarketType"), "group_item": market.get("groupItemTitle"),
            "resolution_source": market.get("resolutionSource"), "uma_status": market.get("umaResolutionStatus"),
            "automatic": market.get("automaticallyResolved"), "tick": market.get("orderPriceMinTickSize"),
            "event_id": str(event.get("id")) if event.get("id") is not None else None,
            "event_slug": event.get("slug"), "series": event.get("seriesSlug")}


def backfill_markets(directory: Path, days: float, minimum_volume: float, workers: int) -> int:
    """List resolved markets hour by hour; offsets stay small inside one hour of end dates."""
    path, done_path = directory / "markets.jsonl", directory / "market-hours-done.jsonl"
    seen = {row["id"] for row in read_rows(path)}
    done = {row["hour"] for row in read_rows(done_path)}
    now = int(time.time())
    last = (now - 6 * 3600) // 3600 * 3600        # leave recent hours time to resolve
    hours = [hour for hour in range((now - int(days * 86400)) // 3600 * 3600, last, 3600) if hour not in done]

    def one(hour: int) -> None:
        rows, offset = [], 0
        try:
            while True:
                page = fetch_json(f"{GAMMA}/markets?closed=true&limit=100&offset={offset}&order=endDate&ascending=true"
                                  f"&end_date_min={stamp(hour)}&end_date_max={stamp(hour + 3599)}"
                                  f"&volume_num_min={minimum_volume:g}")
                if not isinstance(page, list):
                    raise RuntimeError("unexpected listing shape")
                rows.extend(row for row in map(market_row, page) if row)
                if len(page) < 100:
                    break
                offset += 100
        except RuntimeError as exc:
            append(directory / "markets-missing.jsonl", {"hour": hour, "error": str(exc)[:200]})
            return
        for row in rows:
            if row["id"] not in seen:
                seen.add(row["id"])
                append(path, row)
        append(done_path, {"hour": hour, "markets": len(rows)})

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, hours))
    return len(hours)


def backfill_prices(directory: Path, selection: Path, lookback_days: float, fidelity: int, workers: int) -> int:
    """Price history of the first outcome token for the market ids listed in `selection`."""
    path = directory / f"prices-f{fidelity}.jsonl"
    done = {row["id"] for row in read_rows(path)}
    wanted = set(json.loads(selection.read_text(encoding="utf-8")))
    todo = [row for row in read_rows(directory / "markets.jsonl") if row["id"] in wanted and row["id"] not in done]

    def one(market: dict) -> None:
        anchor = max(value for value in (market["end"], market["closed_at"], market["game_start"]) if value)
        first = max(market["start"] or 0, anchor - int(lookback_days * 86400)) - 3600
        try:
            raw = fetch_json(f"{CLOB}/prices-history?market={market['tokens'][0]}"
                             f"&startTs={first}&endTs={anchor + 3600}&fidelity={fidelity}")
            history = [[int(point["t"]), float(point["p"])] for point in raw.get("history") or []]
            append(path, {"id": market["id"], "history": history})
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            append(directory / "prices-missing.jsonl", {"id": market["id"], "error": str(exc)[:200]})

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, todo))
    return len(todo)


def complete_events(directory: Path, selection: Path, workers: int) -> int:
    """Add every market of the listed event slugs, whatever its volume.

    The hourly listing keeps only markets above a volume floor, and a bucket's final volume
    depends on how the event unfolded. Calibration on such a subset is biased; events listed
    in complete-events.jsonl have all their markets on file.
    """
    path, done_path = directory / "markets.jsonl", directory / "complete-events.jsonl"
    seen = {row["id"] for row in read_rows(path)}
    done = {row["slug"] for row in read_rows(done_path)}
    todo = [slug for slug in json.loads(selection.read_text(encoding="utf-8")) if slug not in done]

    def one(slug: str) -> None:
        try:
            event = fetch_json(f"{GAMMA}/events/slug/{slug}")
            markets = event.get("markets") or []
            rows = []
            for market in markets:
                market["events"] = [{"id": event.get("id"), "slug": event.get("slug"), "seriesSlug": event.get("seriesSlug")}]
                row = market_row(market)
                if row:
                    rows.append(row)
        except (RuntimeError, AttributeError, KeyError, TypeError) as exc:
            append(directory / "complete-missing.jsonl", {"slug": slug, "error": str(exc)[:200]})
            return
        for row in rows:
            if row["id"] not in seen:
                seen.add(row["id"])
                append(path, row)
        append(done_path, {"slug": slug, "markets": len(rows)})

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, todo))
    return len(todo)


def backfill_trades(directory: Path, selection: Path, workers: int) -> int:
    """Taker trades of every market in the listed event slugs, one request per event.

    One line per market: [timestamp, outcome_index, taker_is_buyer, price, size, taker], oldest first;
    `taker` is the first 12 hex digits of the taker's public proxy wallet (enough to tell wallets apart).
    Markets of the event that never traded get an empty list, so absence means "not fetched".
    """
    path, done_path = directory / "trades.jsonl", directory / "trades-events-done.jsonl"
    done = {row["slug"] for row in read_rows(done_path)}
    by_event: dict[str, list[dict]] = {}
    for row in read_rows(directory / "markets.jsonl"):
        if row.get("event_slug") and row.get("event_id"):
            by_event.setdefault(row["event_slug"], []).append(row)
    todo = [slug for slug in json.loads(selection.read_text(encoding="utf-8")) if slug in by_event and slug not in done]

    def one(slug: str) -> None:
        markets = by_event[slug]
        owner = {market["condition_id"]: market["id"] for market in markets}
        trades: dict[str, list] = {market["id"]: [] for market in markets}
        offset = 0
        try:
            while True:
                page = fetch_json(f"{DATA}/trades?eventId={markets[0]['event_id']}&limit=10000&offset={offset}&takerOnly=true",
                                  timeout=120)
                if not isinstance(page, list):
                    raise RuntimeError("unexpected trades shape")
                for item in page:
                    market_id = owner.get(item.get("conditionId"))
                    if market_id is not None:
                        trades[market_id].append([int(item["timestamp"]), int(item["outcomeIndex"]), int(item["side"] == "BUY"),
                                                  float(item["price"]), float(item["size"]),
                                                  (item.get("proxyWallet") or "")[2:14].lower()])
                if len(page) < 10000:
                    break
                offset += 10000
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            append(directory / "trades-missing.jsonl", {"slug": slug, "error": str(exc)[:200]})
            return
        for market_id, rows in trades.items():
            append(path, {"id": market_id, "trades": sorted(rows)})
        append(done_path, {"slug": slug, "markets": len(trades), "trades": sum(len(rows) for rows in trades.values())})

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, todo))
    return len(todo)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("markets", "prices", "complete", "trades"))
    parser.add_argument("--directory", type=Path, default=Path(".polycast/slow-market-backfill-v1"))
    parser.add_argument("--days", type=float, default=45)
    parser.add_argument("--minimum-volume", type=float, default=10000)
    parser.add_argument("--selection", type=Path, help="JSON list of market ids to fetch prices for")
    parser.add_argument("--lookback-days", type=float, default=14)
    parser.add_argument("--fidelity", type=int, default=60, help="minutes between price samples")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    started = time.time()
    if args.stage == "markets":
        summary = {"hours_requested": backfill_markets(args.directory, args.days, args.minimum_volume, args.workers)}
    elif args.stage == "complete":
        summary = {"events_requested": complete_events(args.directory, args.selection, args.workers)}
    elif args.stage == "trades":
        summary = {"trades_requested": backfill_trades(args.directory, args.selection, args.workers)}
    else:
        summary = {"prices_requested": backfill_prices(args.directory, args.selection, args.lookback_days,
                                                       args.fidelity, args.workers)}
    summary["seconds"] = round(time.time() - started, 1)
    summary["markets_total"] = len(read_rows(args.directory / "markets.jsonl"))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
