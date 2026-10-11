"""Official, market-specific snapshots for hourly crypto and daily MLB research."""
from __future__ import annotations

import math
import statistics
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from .market import parse_utc
from .net import fetch_bytes, fetch_json
from .venue_status import COINBASE_STATUS, coinbase_status_evidence
from .store import _upsert_source, insert_evidence, now_iso
from .paper import settle
from .market import _list, GAMMA, CHAINLINK_BTC_TWAP
from .twap_records import read_twap_records

BINANCE = "https://data-api.binance.vision/api/v3/klines"
MLB = "https://statsapi.mlb.com/api/v1/schedule"
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD"


def _diffusion_units(opening: float, latest: float, sigma_1m: float,
                     remaining_minutes: float) -> dict:
    """Illustrative diffusion scale, not calibrated future TWAP uncertainty."""
    inputs = (opening, latest, sigma_1m, remaining_minutes)
    if any(not math.isfinite(x) or x <= 0 for x in inputs):
        raise ValueError("Diffusion inputs must be finite and positive")
    log_scale = sigma_1m * math.sqrt(remaining_minutes)
    z = math.log(latest / opening) / log_scale
    return {"horizon_log_stdev": log_scale, "horizon_usd_stdev": latest * log_scale,
            "z": z, "p_up": min(.99, max(.01, statistics.NormalDist().cdf(z))),
            "future_twap_variance_identified": False}


def _twap_points(path: Path, start: datetime, now: datetime,
                 *, available_at: datetime | None = None) -> list[tuple[int, Decimal]]:
    points, _ = read_twap_records(path, start, now, available_at=available_at)
    return [(point["timestamp_ms"], point["price"]) for point in points]


def _twap_snapshot(market: dict, now: datetime, path: Path | None) -> dict | None:
    if path is None or market.get("category") != "crypto5m":
        return None
    start = datetime.fromtimestamp(int(market["source_ref"].split(":")[1]), timezone.utc)
    end = parse_utc(market["end_at"])
    if end is None:
        return None
    records, receipt_audit = read_twap_records(path, start - timedelta(minutes=35), now)
    history = [(point["timestamp_ms"], point["price"]) for point in records]
    start_ms = int(start.timestamp() * 1000)
    points = [row for row in history if row[0] >= start_ms - 3000]
    if not points:
        return None
    opening = min(points, key=lambda row: abs(row[0] - start_ms))
    latest = points[-1]
    if (abs(opening[0] - start_ms) > 3000 or
            int(now.timestamp() * 1000) - latest[0] > 30000 or
            latest[0] <= opening[0] + 15000):
        return None
    opening_at = datetime.fromtimestamp(opening[0] / 1000, timezone.utc).isoformat(timespec="seconds")
    latest_at = datetime.fromtimestamp(latest[0] / 1000, timezone.utc).isoformat(timespec="seconds")
    near_start = [value for timestamp, value in history if abs(timestamp - start_ms) <= 3000]
    near_span = max(near_start) - min(near_start)
    minute_prices: dict[int, Decimal] = {}
    for timestamp, value in history:
        minute, offset = divmod(timestamp, 60000)
        if offset <= 3000 and minute not in minute_prices:
            minute_prices[minute] = value
    changes = [math.log(float(minute_prices[minute] / minute_prices[minute - 1]))
               for minute in sorted(minute_prices) if minute - 1 in minute_prices]
    sigma = statistics.stdev(changes) if len(changes) >= 8 else None
    remaining = max(1 / 60, (end - now).total_seconds() / 60)
    relay_probability = None
    units = None
    twap_velocity = 0.0
    if len(points) >= 2:
        latest_t, latest_v = points[-1]
        prior = next((p for p in reversed(points[:-1]) if 10000 <= latest_t - p[0] <= 30000), points[-2])
        dt = (latest_t - prior[0]) / 1000.0
        if dt > 0:
            twap_velocity = float(latest_v - prior[1]) / dt
    if sigma is not None and sigma > 0:
        units = _diffusion_units(float(opening[1]), float(latest[1]), sigma, remaining)
        relay_probability = units["p_up"]
    numeric_context = (f" Opening sample offset {(opening[0] - start_ms) / 1000:+.0f}s; "
                       f"observed +/-3s local range {near_span:.2f} across {len(near_start)} points "
                       "(not a bound on the signed opening reference); "
                       f"latest sample age {(int(now.timestamp() * 1000) - latest[0]) / 1000:.0f}s; "
                       f"latest minus opening proxy {latest[1] - opening[1]:+.2f}. ")
    if relay_probability is not None:
        numeric_context += (f"Recent {len(changes)} consecutive 1m RTDS TWAP log returns "
                            f"stdev {sigma:.8f}; illustrative same-feed diffusion proxy "
                            f"P(Up) {relay_probability:.4f} over {remaining:.2f}m remaining. "
                            f"Remaining horizon log stdev {units['horizon_log_stdev']:.8f}; "
                            f"linearized dollar stdev {units['horizon_usd_stdev']:.2f}; "
                            f"log-diffusion z {units['z']:.4f}. "
                            f"Rolling TWAP velocity {twap_velocity:+.4f} USD/s. "
                            "Sigma is estimated from an already averaged TWAP relay. Future final-60s TWAP variance is not identified; "
                            "do not apply an additional 1/sqrt(3) factor or interpret proxy z as calibrated significance. "
                            "Units: sigma is per one-minute log return; horizon stdev uses "
                            "sigma_1m * sqrt(remaining_minutes), with no division by 60. "
                            "These horizon values use snapshot time, and are illustrative diffusion "
                            "scales, not a calibrated variance of future 60-second TWAP. "
                            "This proxy is uncalibrated and does not include signed-reference error. ")
    else:
        numeric_context += "Insufficient consecutive 1m RTDS TWAP history for a same-feed volatility estimate. "
    return {"source": "Chainlink TWAP via Polymarket RTDS",
            "source_id": "chainlink_btc_twap60", "feed_url": CHAINLINK_BTC_TWAP,
            "license_scope": "official_source",
            "url": CHAINLINK_BTC_TWAP + f"#market-{market['id']}",
            "origin_story_id": market["source_ref"],
            "title": f"BTC/USD 60s TWAP live stream for 5m market {market['id']}",
            "summary": (f"Polymarket relay provides Chainlink BTC/USD 60s TWAP. "
                        f"Near window start {opening_at}: {opening[1]} (proxy for opening reference, not independently verified as exact official price-to-beat); "
                        f"latest observation {latest_at}: {latest[1]}. " + numeric_context +
                        "These are time-stamped live relay values, not independently verified signed reports. "
                        "Final outcome must still be checked against the published market resolution."),
            "published_at": latest_at, "updated_at": latest_at,
            "twap_price": float(latest[1]), "twap_velocity": twap_velocity,
            "receipt_audit": receipt_audit,
            "latest_received_at_ms": records[-1]["received_at_ms"],
            "feed_protocol": records[-1]["feed_protocol"]}


def _twap_winner(market: dict, path: Path | None) -> str | None:
    if path is None:
        return None
    start = datetime.fromtimestamp(int(market["source_ref"].split(":")[1]), timezone.utc)
    end = parse_utc(market["end_at"])
    if end is None:
        return None
    points = _twap_points(path, start, end + timedelta(seconds=3),
                          available_at=datetime.now(timezone.utc))
    if not points:
        return None
    opening = min(points, key=lambda row: abs(row[0] - int(start.timestamp() * 1000)))
    closing = min(points, key=lambda row: abs(row[0] - int(end.timestamp() * 1000)))
    if (abs(opening[0] - int(start.timestamp() * 1000)) > 3000 or
            abs(closing[0] - int(end.timestamp() * 1000)) > 3000 or
            closing[0] <= opening[0]):
        return None
    return "YES" if closing[1] >= opening[1] else "NO"


def _btc_5m_snapshot(market: dict, now: datetime) -> dict | None:
    symbol, start_text = market["source_ref"].split(":", 1)
    if symbol != "BTCUSD":
        return None
    start = datetime.fromtimestamp(int(start_text), timezone.utc)
    end = parse_utc(market["end_at"])
    if end != start + timedelta(minutes=5) or not start + timedelta(seconds=20) <= now < end - timedelta(seconds=45):
        return None
    candles_url = COINBASE + "/candles?" + urlencode({
        "granularity": 60, "start": (start - timedelta(minutes=90)).isoformat(),
        "end": now.isoformat()})
    candles = fetch_json(candles_url)
    ticker = fetch_json(COINBASE + "/ticker")
    by_start = {int(c[0]): c for c in candles if isinstance(c, list) and len(c) >= 6}
    opening_candle = by_start.get(int(start.timestamp()))
    ticker_time = parse_utc(ticker.get("time"))
    if opening_candle is None or ticker_time is None or not start <= ticker_time <= now + timedelta(seconds=5):
        return None
    opening, current = float(opening_candle[3]), float(ticker["price"])
    closes = [float(c[4]) for timestamp, c in sorted(by_start.items())
              if start - timedelta(minutes=75) <= datetime.fromtimestamp(timestamp, timezone.utc) < start]
    if len(closes) < 45 or any(not math.isfinite(x) or x <= 0 for x in closes):
        return None
    changes = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    sigma = statistics.stdev(changes)
    if min(opening, current, sigma) <= 0 or not math.isfinite(sigma):
        return None
    remaining = max(1, (end - ticker_time).total_seconds() / 60)
    z = math.log(current / opening) / (sigma * math.sqrt(remaining))
    proxy_probability = min(.99, max(.01, statistics.NormalDist().cdf(z)))
    observed = ticker_time.isoformat(timespec="seconds")
    return {"source": "Coinbase Exchange BTC-USD", "source_id": "coinbase_btc_5m",
            "feed_url": COINBASE, "license_scope": "official_source", "url": candles_url,
            "origin_story_id": market["source_ref"],
            "title": f"Coinbase BTC-USD proxy snapshot for 5m market {market['id']}",
            "summary": (f"Coinbase BTC-USD market opens {start.isoformat()} near {opening:.2f}; "
                        f"last trade at {observed} was {current:.2f}; "
                        f"previous {len(changes)} completed 1m log returns stdev {sigma:.8f}; "
                        f"spot-diffusion proxy P(Up) {proxy_probability:.4f}. "
                        "Polymarket resolves on Chainlink BTC/USD TWAP 60s, not Coinbase spot. "
                        "Opening, closing, and probability are proxy features only, never settlement truth."),
            "published_at": observed, "updated_at": observed,
            "spot_price": current}


def hourly_window_ready(market: dict, now: datetime) -> bool:
    """An hourly forecast needs the opening minute to have actually closed."""
    try:
        symbol, start_text = market["source_ref"].split(":", 1)
        start = datetime.fromtimestamp(int(start_text), timezone.utc)
        end = parse_utc(market["end_at"])
        return (symbol in ("BTCUSDT", "ETHUSDT") and end == start + timedelta(hours=1)
                and start + timedelta(minutes=1) <= now < end)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return False


def _crypto_snapshot(market: dict, now: datetime) -> dict | None:
    if not hourly_window_ready(market, now):
        return None
    symbol, start_text = market["source_ref"].split(":", 1)
    start = datetime.fromtimestamp(int(start_text), timezone.utc)
    end = parse_utc(market["end_at"])
    # Request only closed 1-minute candles; the market settles against the 1-hour candle.
    last_closed = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    url = BINANCE + "?" + urlencode({"symbol": symbol, "interval": "1m",
        "startTime": int((start - timedelta(minutes=120)).timestamp() * 1000),
        "endTime": int(last_closed.timestamp() * 1000), "limit": 1000})
    candles = fetch_json(url)
    by_start = {int(c[0]): c for c in candles if isinstance(c, list) and len(c) >= 7}
    first = by_start.get(int(start.timestamp() * 1000))
    latest = by_start.get(int(last_closed.timestamp() * 1000))
    if first is None or latest is None:
        return None
    # Binance identifies each 1m kline by its opening millisecond; index 6 is
    # its closing millisecond. Validate both so the URL endTime/open-time and
    # the quoted minute-end observation cannot be mistaken for a future bar.
    try:
        boundaries_match = (int(first[6]) == int(start.timestamp() * 1000) + 59999 and
                            int(latest[6]) == int(last_closed.timestamp() * 1000) + 59999)
    except (ValueError, TypeError, OverflowError):
        return None
    if not boundaries_match:
        return None
    prior = [float(c[4]) for c in sorted(candles, key=lambda c: c[0])
             if start - timedelta(minutes=120) <= datetime.fromtimestamp(c[0]/1000, timezone.utc) <= last_closed]
    if len(prior) < 65 or any(x <= 0 or not math.isfinite(x) for x in prior):
        return None
    minute_returns = [math.log(b / a) for a, b in zip(prior, prior[1:])][-120:]
    sigma = statistics.stdev(minute_returns)
    opening, current = float(first[1]), float(latest[4])
    if min(opening, current, sigma) <= 0:
        return None
    remaining = max(1, (end - now).total_seconds() / 60)
    z = math.log(current / opening) / (sigma * math.sqrt(remaining))
    normal_probability = min(.99, max(.01, statistics.NormalDist().cdf(z)))
    observed = (last_closed + timedelta(minutes=1)).isoformat(timespec="seconds")
    return {"source": "Binance spot", "source_id": "binance_klines",
        "feed_url": BINANCE, "license_scope": "official_source", "url": url,
        "origin_story_id": market["source_ref"],
        "title": f"{symbol} hourly candle snapshot for market {market['id']}",
        "summary": (f"Binance {symbol} 1h candle opens {start.isoformat()} at {opening:.8f}; "
                    f"latest returned 1m candle has open timestamp {last_closed.isoformat(timespec='seconds')} "
                    f"(the request endTime) and close timestamp "
                    f"{datetime.fromtimestamp(int(latest[6]) / 1000, timezone.utc).isoformat(timespec='milliseconds')}; "
                    f"that completed minute ends {observed} at {current:.8f}; "
                    f"recent {len(minute_returns)} 1m log-return sample stdev {sigma:.8f}; "
                    f"normal diffusion baseline P(Up) {normal_probability:.4f}. "
                    "This is a model estimate, not a guaranteed edge; verify exchange data continuity."),
        "published_at": observed, "updated_at": observed}


def _mlb_snapshot(market: dict, now: datetime) -> dict | None:
    if not market["source_ref"].isdigit():
        return None
    start = parse_utc(market["end_at"])
    if start is None or now >= start:
        return None
    url = MLB + "?" + urlencode({"gamePk": market["source_ref"],
                                 "hydrate": "team,probablePitcher"})
    payload = fetch_json(url)
    games = [game for day in payload.get("dates", []) for game in day.get("games", [])]
    game = next((item for item in games if str(item.get("gamePk")) == market["source_ref"]), None)
    if game is None or parse_utc(game.get("gameDate")) != start:
        return None
    if game.get("status", {}).get("abstractGameState") not in ("Preview", "Pre-Game"):
        return None
    parts = []
    for side in ("away", "home"):
        team = game["teams"][side]
        if team["team"]["name"] != market["yes_label" if side == "away" else "no_label"]:
            return None
        record = team.get("leagueRecord") or {}
        pitcher = team.get("probablePitcher") or {}
        parts.append(f"{side}: {team['team']['name']}, wins {record.get('wins', '?')}, "
                     f"losses {record.get('losses', '?')}, probable pitcher "
                     f"{pitcher.get('fullName', 'unannounced')}")
    # The schedule lacks a release timestamp; record first local observation.
    timestamp = now.isoformat(timespec="seconds")
    return {"source": "MLB Stats API", "source_id": "mlb_schedule",
        "feed_url": MLB, "license_scope": "official_source", "url": url,
        "origin_story_id": market["source_ref"],
        "title": f"MLB official pregame schedule for {market['event_title']}",
        "summary": (f"Game {market['source_ref']} starts {start.isoformat()}; " + "; ".join(parts) +
                    ". Records are descriptive and do not by themselves establish a betting edge."),
        "published_at": timestamp, "updated_at": timestamp}


def collect_short(conn, markets: list[dict], chainlink_path: Path | None = None) -> dict:
    now = datetime.now(timezone.utc)
    for source_id, name, url in (("binance_klines", "Binance spot", BINANCE),
                                 ("mlb_schedule", "MLB Stats API", MLB),
                                 ("coinbase_btc_5m", "Coinbase Exchange BTC-USD", COINBASE),
                                 ("coinbase_exchange_status", "Coinbase Exchange service status", COINBASE_STATUS),
                                 ("chainlink_btc_twap60", "Chainlink TWAP via Polymarket RTDS", CHAINLINK_BTC_TWAP)):
        _upsert_source(conn, source_id, name, "official", url,
                       "official_source", url, "", 1)
    # Persist metadata before slow external reads; no partial evidence has
    # been written yet, and the collected versions are saved together below.
    conn.commit()
    items, errors = [], []
    status_payload, status_observed, status_attempted = None, None, False
    for market in markets:
        market_items = []
        try:
            item = (_crypto_snapshot(market, now) if market.get("category") == "crypto" else
                    _btc_5m_snapshot(market, now) if market.get("category") == "crypto5m" else
                    _mlb_snapshot(market, now) if market.get("category") == "daily" else None)
            if item:
                if market.get("category") == "daily":
                    prior = conn.execute("""SELECT e.summary,e.published_at,e.updated_at,e.first_seen_at
                        FROM evidence_current c JOIN evidence e ON e.id=c.evidence_id
                        WHERE c.url=?""", (item["url"],)).fetchone()
                    if prior and prior["summary"] == item["summary"]:
                        observed = max(prior["published_at"], prior["first_seen_at"])
                        item["published_at"] = observed
                        item["updated_at"] = observed
                market_items.append(item)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            errors.append(f"{market['id']}: {type(exc).__name__}: {exc}"[:200])
        try:
            twap = _twap_snapshot(market, now, chainlink_path)
            if twap:
                market_items.append(twap)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            errors.append(f"{market['id']}: {type(exc).__name__}: {exc}"[:200])
        if market.get("category") == "crypto5m" and market_items:
            if not status_attempted:
                status_attempted = True
                try:
                    status_payload = json.loads(fetch_bytes(COINBASE_STATUS, timeout=3))
                    status_observed = datetime.now(timezone.utc)
                except (ValueError, RuntimeError) as exc:
                    errors.append(f"Coinbase Exchange status: {type(exc).__name__}"[:200])
            if status_payload is not None:
                try:
                    market_items.append(coinbase_status_evidence(status_payload, market, status_observed))
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    errors.append(f"Coinbase Exchange status: {type(exc).__name__}: {exc}"[:200])
        if market.get("category") == "crypto5m":
            spot_item = next((it for it in market_items if it.get("source_id") == "coinbase_btc_5m"), None)
            twap_item = next((it for it in market_items if it.get("source_id") == "chainlink_btc_twap60"), None)
            if spot_item and twap_item and "spot_price" in spot_item and "twap_price" in twap_item:
                spot_p = spot_item["spot_price"]
                twap_p = twap_item["twap_price"]
                basis = spot_p - twap_p
                twap_vel = twap_item.get("twap_velocity", 0.0)
                basis_note = (f" Spot-TWAP rolling basis {basis:+.2f} USD (Coinbase spot {spot_p:.2f} vs RTDS TWAP {twap_p:.2f}); "
                              f"TWAP rolling velocity {twap_vel:+.3f} USD/s. ")
                twap_item["summary"] += basis_note
        items.extend(market_items)
    return {"fetched": len(items), "new_versions": insert_evidence(conn, items),
            "errors": errors}


def _official_winner(market: dict, now: datetime) -> tuple[str, str] | None:
    """Return side and independent verification URL only for an exact binary result."""
    if market["category"] == "crypto":
        symbol, start_text = market["source_ref"].split(":", 1)
        if symbol not in ("BTCUSDT", "ETHUSDT"):
            return None
        start = int(start_text) * 1000
        url = BINANCE + "?" + urlencode({"symbol": symbol, "interval": "1h",
                                         "startTime": start, "limit": 1})
        candles = fetch_json(url)
        if len(candles) != 1 or int(candles[0][0]) != start or int(candles[0][6]) >= int(now.timestamp()*1000):
            return None
        opening, closing = float(candles[0][1]), float(candles[0][4])
        if not all(math.isfinite(x) and x > 0 for x in (opening, closing)):
            return None
        return ("YES" if closing >= opening else "NO"), url
    if market["category"] == "daily":
        url = MLB + "?" + urlencode({"gamePk": market["source_ref"], "hydrate": "team"})
        payload = fetch_json(url)
        games = [game for day in payload.get("dates", []) for game in day.get("games", [])]
        game = next((item for item in games if str(item.get("gamePk")) == market["source_ref"]), None)
        if not game or game.get("status", {}).get("abstractGameState") != "Final":
            return None
        away, home = game["teams"]["away"], game["teams"]["home"]
        if (away["team"]["name"] != market["yes_label"] or
                home["team"]["name"] != market["no_label"] or
                parse_utc(game.get("gameDate")) != parse_utc(market["end_at"])):
            return None
        if away.get("score") is None or home.get("score") is None or away["score"] == home["score"]:
            return None  # 50/50, postponement, and tie need a separate payout model.
        return ("YES" if away["score"] > home["score"] else "NO"), url
    return None


def _settlement_check(conn, market_id: str, status: str, reason: str,
                      source_url: str, official_side: str | None = None) -> None:
    conn.execute("""INSERT INTO settlement_checks
        (market_id,checked_at,status,reason,source_url,official_side) VALUES(?,?,?,?,?,?)
        ON CONFLICT(market_id) DO UPDATE SET checked_at=excluded.checked_at,
        status=excluded.status,reason=excluded.reason,source_url=excluded.source_url,
        official_side=excluded.official_side""",
        (market_id, now_iso(), status, reason, source_url, official_side))
    # Diagnostic writes must not hold the writer lock through the next HTTP read.
    conn.commit()


def reconcile_short(conn, limit: int = 40,
                    categories: tuple[str, ...] = ("crypto", "crypto5m", "daily"),
                    chainlink_path: Path | None = None) -> dict:
    """Settle after Gamma final agrees with the market's source observations."""
    if not categories or any(category not in ("crypto", "crypto5m", "daily") for category in categories):
        raise ValueError("Unsupported short-cycle category")
    conn.commit()
    now = datetime.now(timezone.utc)
    placeholders = ",".join("?" for _ in categories)
    rows = conn.execute(f"""SELECT m.* FROM markets m LEFT JOIN resolutions r ON r.market_id=m.id
        WHERE m.category IN ({placeholders}) AND r.market_id IS NULL AND m.end_at<?
        AND m.end_at>? ORDER BY m.end_at DESC LIMIT ?""",
        (*categories, (now - timedelta(minutes=2)).isoformat(),
         (now - timedelta(days=7)).isoformat(), limit)).fetchall()
    settled, pending, errors = [], [], []
    for row in rows:
        market = dict(row)
        try:
            gamma_url = f"{GAMMA}/events/slug/{market['event_slug']}"
            event = fetch_json(gamma_url)
            remote = next((item for item in event.get("markets", [])
                           if str(item.get("id")) == market["id"]), None)
            if (str(event.get("id")) != market["event_id"] or remote is None or
                    not event.get("closed") or not remote.get("closed") or
                    (remote.get("description") or "") != market["description"] or
                    _list(remote.get("outcomes")) != [market["yes_label"], market["no_label"]] or
                    _list(remote.get("clobTokenIds")) != [market["yes_token"], market["no_token"]]):
                pending.append(market["id"])
                _settlement_check(conn, market["id"], "pending", "official_result_pending", gamma_url)
                continue
            if ((market["category"] == "crypto5m" and
                 (parse_utc(remote.get("endDate")) != parse_utc(market["end_at"]) or
                  remote.get("resolutionSource") != CHAINLINK_BTC_TWAP or
                  (remote.get("cryptoMarketConfig") or {}).get("id") != "btc-5m-twap-60" or
                  (remote.get("cryptoMarketConfig") or {}).get("twapLookbackSeconds") != 60 or
                  market["source_ref"] != market["event_slug"].replace("btc-updown-5m-", "BTCUSD:"))) or
                (market["category"] == "crypto" and
                 (parse_utc(remote.get("endDate")) != parse_utc(market["end_at"]) or
                  remote.get("resolutionSource") !=
                  f"https://www.binance.com/en/trade/{market['source_ref'].split(':')[0][:-4]}_USDT")) or
                (market["category"] == "daily" and
                 (parse_utc(remote.get("gameStartTime")) != parse_utc(market["end_at"]) or
                  remote.get("resolutionSource") != "https://www.mlb.com/"))):
                pending.append(market["id"])
                _settlement_check(conn, market["id"], "pending", "contract_mismatch", gamma_url)
                continue
            close_time = parse_utc(remote.get("closedTime") or event.get("closedTime"))
            prices = _list(remote.get("outcomePrices"))
            confirmation_delay = timedelta(minutes=2 if market["category"] == "crypto5m" else 10)
            if (close_time is None or close_time > now - confirmation_delay or
                    close_time <= parse_utc(market["end_at"]) or
                    prices not in (["1", "0"], ["0", "1"])):
                pending.append(market["id"])
                _settlement_check(conn, market["id"], "pending", "confirmation_pending", gamma_url)
                continue
            gamma_side = "YES" if prices == ["1", "0"] else "NO"
            if market["category"] == "crypto5m":
                relay_side = _twap_winner(market, chainlink_path)
                if relay_side is None or relay_side != gamma_side:
                    pending.append(market["id"])
                    _settlement_check(conn, market["id"], "pending",
                        "twap_boundary_missing" if relay_side is None else "source_conflict",
                        gamma_url, gamma_side)
                    continue
                corroboration = (relay_side, CHAINLINK_BTC_TWAP)
            else:
                corroboration = _official_winner(market, now)
                if corroboration is None or corroboration[0] != gamma_side:
                    pending.append(market["id"])
                    _settlement_check(conn, market["id"], "pending",
                        "corroboration_missing" if corroboration is None else "source_conflict",
                        gamma_url, gamma_side)
                    continue
            count = settle(conn, market["id"], gamma_side, gamma_url,
                           close_time.isoformat(timespec="seconds"))
            _settlement_check(conn, market["id"], "verified", "sources_agree", gamma_url, gamma_side)
            settled.append({"market_id": market["id"], "winning_side": gamma_side,
                            "paper_orders_settled": count,
                            "verification": ("rtds_relay_cross_checked" if market["category"] == "crypto5m"
                                             else "independent_source_cross_checked"),
                            "corroboration_url": corroboration[1]})
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            errors.append(f"{market['id']}: {type(exc).__name__}: {exc}"[:200])
            _settlement_check(conn, market["id"], "error", "external_data_error", gamma_url)
    return {"settled": settled, "pending": pending, "errors": errors}
