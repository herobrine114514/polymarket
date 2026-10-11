"""Backfill public history for resolved BTC 5m TWAP-60 markets. Research only.

Unauthenticated public GETs: Gamma (resolved events and official outcomes), CLOB
prices-history (minute-sampled Up price), Binance 1s klines (spot proxy for the
Chainlink TWAP stream). Stage `book` makes no request: it reads the collector's own
raw archive for the locally received top of book. No orders, no account access, no
production imports. Resumable: finished items are skipped on rerun.
"""
from __future__ import annotations
import argparse
import gzip
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://data-api.binance.vision"
SERIES = "btc-up-or-down-5m"
RULE_ID = "btc-5m-twap-60"
USER_AGENT = "Mozilla/5.0 (compatible; PolycastPaperResearch/0.1; +https://docs.polymarket.com/)"
_write_lock = threading.Lock()


def fetch_json(url: str, *, timeout: float = 30, attempts: int = 5):
    last = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
            return json.loads(data)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(min(20, 1.5 * 2 ** attempt))
    raise RuntimeError(f"{last} from {url}")


def append(path: Path, row: dict) -> None:
    with _write_lock, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def event_row(event: dict) -> dict | None:
    markets = event.get("markets") or []
    slug = event.get("slug") or ""
    if len(markets) != 1 or not slug.startswith("btc-updown-5m-"):
        return None
    market = markets[0]
    start = int(slug.rsplit("-", 1)[1])
    tokens, outcomes = json.loads(market["clobTokenIds"]), json.loads(market["outcomes"])
    prices = json.loads(market.get("outcomePrices") or "[]")
    return {"start": start, "market_id": str(market["id"]), "condition_id": market["conditionId"],
            "up_token": tokens[0], "down_token": tokens[1], "outcomes": outcomes, "outcome_prices": prices,
            "rule_id": (market.get("cryptoMarketConfig") or {}).get("id"),
            "resolution_source": market.get("resolutionSource"), "end_date": market.get("endDate"),
            "closed": market.get("closed"), "closed_time": market.get("closedTime"),
            "uma_status": market.get("umaResolutionStatus"), "volume": market.get("volumeNum"),
            "fee_rate": (market.get("feeSchedule") or {}).get("rate"),
            "fee_exponent": (market.get("feeSchedule") or {}).get("exponent"),
            "tick": market.get("orderPriceMinTickSize"), "min_size": market.get("orderMinSize")}


def backfill_events(directory: Path, days: float, workers: int) -> int:
    """Enumerate every 300s slot by slug; the list endpoint is not relied on for completeness."""
    path = directory / "events.jsonl"
    done = {row["start"] for row in read_rows(path)}
    missing_path = directory / "events-missing.jsonl"
    now = int(time.time())
    last = now // 300 * 300 - 900  # leave the newest slots time to resolve
    first = (now - int(days * 86400)) // 300 * 300
    # Fast path: the series listing returns 100 resolved events per request. Offsets are
    # capped near 2000, so page with an end-date cursor instead.
    cursor = last + 300
    while cursor > first:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cursor))
        page = fetch_json(f"{GAMMA}/events?series_slug={SERIES}&closed=true&limit=100"
                          f"&order=endDate&ascending=false&end_date_max={stamp}")
        rows = [row for row in map(event_row, page if isinstance(page, list) else []) if row]
        for row in rows:
            if (first <= row["start"] <= last and row["start"] not in done and row["closed"]
                    and row["outcome_prices"] in (["1", "0"], ["0", "1"])):
                append(path, row)
                done.add(row["start"])
        oldest = min((row["start"] for row in rows), default=first)
        if not rows or oldest + 300 >= cursor:
            break
        cursor = oldest + 300
    todo = [start for start in range(first, last + 1, 300) if start not in done]

    def one(start: int) -> None:
        try:
            event = fetch_json(f"{GAMMA}/events/slug/btc-updown-5m-{start}")
            row = event_row(event) if isinstance(event, dict) else None
            if row is None or row["start"] != start:
                raise RuntimeError("unexpected event shape")
            if not row["closed"] or row["outcome_prices"] not in (["1", "0"], ["0", "1"]):
                return  # unresolved; retried on the next run
            append(path, row)
        except RuntimeError as exc:
            append(missing_path, {"start": start, "error": str(exc)[:200], "at": int(time.time())})

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, todo))
    return len(todo)


def backfill_prices(directory: Path, workers: int) -> int:
    path = directory / "prices.jsonl"
    done = {row["start"] for row in read_rows(path)}
    todo = [row for row in read_rows(directory / "events.jsonl")
            if row["start"] not in done and row["rule_id"] == RULE_ID]

    def one(event: dict) -> None:
        start = event["start"]
        try:
            raw = fetch_json(f"{CLOB}/prices-history?market={event['up_token']}"
                             f"&startTs={start - 180}&endTs={start + 420}&fidelity=1")
            history = [[int(point["t"]), float(point["p"])] for point in raw.get("history") or []]
            append(path, {"start": start, "fetched_at": int(time.time()), "history": history})
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            append(directory / "prices-missing.jsonl", {"start": start, "error": str(exc)[:200]})

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, todo))
    return len(todo)


def backfill_klines(directory: Path, days: float, workers: int, symbol: str = "BTCUSDT") -> int:
    """One file per 1000 seconds: [open_s, open, high, low, close, volume, taker_buy_volume, trades]."""
    folder = directory / f"klines-1s-{symbol}"
    folder.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    first = (now - int(days * 86400) - 900) // 1000 * 1000
    todo = [chunk for chunk in range(first, (now - 120) // 1000 * 1000, 1000)
            if not (folder / f"{chunk}.json").is_file()]
    todo.reverse()  # newest first, so a second run can meet an older one in the middle

    def one(chunk: int) -> None:
        if (folder / f"{chunk}.json").is_file():
            return
        try:
            raw = fetch_json(f"{BINANCE}/api/v3/klines?symbol={symbol}&interval=1s"
                             f"&startTime={chunk * 1000}&endTime={chunk * 1000 + 999999}&limit=1000")
            rows = [[int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                     float(k[5]), float(k[9]), int(k[8])] for k in raw]
            temporary = folder / f"{chunk}.json.tmp"
            temporary.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
            temporary.replace(folder / f"{chunk}.json")
        except (RuntimeError, KeyError, TypeError, ValueError, IndexError) as exc:
            append(directory / "klines-missing.jsonl", {"chunk": chunk, "error": str(exc)[:200]})

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, todo))
    return len(todo)


def archive_snapshots(path: str) -> list[dict]:
    """Per-second top of book from one local microstructure archive file (read-only)."""
    rows = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if '"feature_snapshot"' not in line[:40]:
                    continue
                snapshot = json.loads(line)
                polymarket, coinbase = snapshot.get("polymarket") or {}, snapshot.get("coinbase") or {}
                up, down = polymarket.get("up") or {}, polymarket.get("down") or {}
                rows.append({
                    "start": snapshot["market"]["start_at_ms"] // 1000, "as_of_ms": snapshot.get("as_of_utc_ms"),
                    "recv_ms": snapshot.get("received_at_ms"),
                    "offset_ms": (snapshot.get("clock_reference") or {}).get("offset_ms"),
                    "up_bid": up.get("bid"), "up_ask": up.get("ask"), "up_src_ms": up.get("source_timestamp_ms"),
                    "down_bid": down.get("bid"), "down_ask": down.get("ask"),
                    "up_valid": (polymarket.get("up_quality") or {}).get("valid"),
                    "down_valid": (polymarket.get("down_quality") or {}).get("valid"),
                    "cb_mid": (coinbase.get("book") or {}).get("midpoint")})
    except (OSError, EOFError, ValueError, KeyError, TypeError):
        return []   # a file still being written or truncated is picked up on the next run
    return rows


def extract_local_book(directory: Path, archive: Path, workers: int) -> int:
    """Rebuild local-book-snapshots.jsonl from the collector's own raw archive. No network."""
    files = sorted(str(path) for path in archive.glob("raw-*.jsonl.gz"))
    temporary, count = directory / "local-book-snapshots.jsonl.tmp", 0
    with ProcessPoolExecutor(workers) as pool, temporary.open("w", encoding="utf-8") as handle:
        for rows in pool.map(archive_snapshots, files, chunksize=4):
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                count += 1
    temporary.replace(directory / "local-book-snapshots.jsonl")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("events", "prices", "klines", "book", "all"))
    parser.add_argument("--directory", type=Path, default=Path(".polycast/btc5m-backfill-v1"))
    parser.add_argument("--archive", type=Path, default=Path(".polycast/microstructure-v1"))
    parser.add_argument("--days", type=float, default=14)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    started = time.time()
    summary = {}
    if args.stage in ("events", "all"):
        summary["events_requested"] = backfill_events(args.directory, args.days, args.workers)
    if args.stage == "prices":      # optional: only the calibration check reads minute prices
        summary["prices_requested"] = backfill_prices(args.directory, args.workers)
    if args.stage in ("klines", "all"):
        summary["kline_chunks_requested"] = backfill_klines(args.directory, args.days, args.workers)
    if args.stage in ("book", "all"):
        summary["local_book_snapshots"] = extract_local_book(args.directory, args.archive, min(args.workers, 6))
    summary["seconds"] = round(time.time() - started, 1)
    summary["events_total"] = len(read_rows(args.directory / "events.jsonl"))
    summary["prices_total"] = len(read_rows(args.directory / "prices.jsonl"))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
