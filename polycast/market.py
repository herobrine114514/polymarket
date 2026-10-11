from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .net import fetch_json
from .execution import match_levels, fill_totals

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
TAG_IDS = (159, 225)  # Fed, economics; discovered via Gamma tags/slug.
MAX_HORIZON = timedelta(days=90)
FED_MEETING_EVENT = re.compile(r"\b(fed decision|fomc meeting|federal reserve decision)\b", re.I)
FED_MEETING_QUESTION = re.compile(
    r"\b(fed.*(?:increase|decrease).*interest rates|no change in fed interest rates)"
    r".*\bafter\b.*\bmeeting\b", re.I)
FED_OUTCOME_SLOTS = ("decrease_50_plus", "decrease_25", "no_change",
                     "increase_25", "increase_50_plus")


def fed_outcome_slot(question: str) -> str | None:
    text = question.lower()
    if "no change in fed interest rates" in text:
        return "no_change"
    direction = "decrease" if "decrease interest rates" in text else (
        "increase" if "increase interest rates" in text else None)
    if direction is None:
        return None
    if re.search(r"\b50\+\s*bps\b", text):
        return direction + "_50_plus"
    if re.search(r"\b25\s*bps\b", text):
        return direction + "_25"
    return None


def complete_fed_event(markets: list[dict]) -> bool:
    slots = [fed_outcome_slot(market["question"]) for market in markets]
    return len(slots) == len(FED_OUTCOME_SLOTS) and set(slots) == set(FED_OUTCOME_SLOTS)


def supported_policy_market(event_title: str, question: str) -> bool:
    """Only meeting decisions have a supported official evidence/settlement path today."""
    return bool(FED_MEETING_EVENT.search(event_title) and
                FED_MEETING_QUESTION.search(question) and fed_outcome_slot(question))


def _list(value: object) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def discover(limit: int = 20, min_liquidity: float = 5000, *,
             errors: list[str] | None = None) -> list[dict]:
    """Discover each sector independently when an audited error sink is supplied."""
    now = datetime.now(timezone.utc)
    sectors = (
        ("macro", lambda: _discover_macro(min_liquidity, now)),
        ("btc5m", lambda: discover_btc_5m(now)),
        ("hourly_crypto", discover_hourly_crypto),
        ("daily_mlb", discover_daily_mlb),
    )
    found = {}
    for name, operation in sectors:
        try:
            found[name] = operation()
        except Exception as exc:
            if errors is None:
                raise
            errors.append(f"{name}: {type(exc).__name__}: {exc}"[:240])
            found[name] = []
    # Preserve complete Fed brackets while reserving slots for short events.
    return (found["btc5m"] + found["hourly_crypto"] + found["daily_mlb"] + found["macro"])[:limit]


def _discover_macro(min_liquidity: float, now: datetime) -> list[dict]:
    found: dict[str, dict] = {}
    for tag_id in TAG_IDS:
        query = urlencode({"tag_id": tag_id, "closed": "false", "limit": 100})
        payload = fetch_json(f"{GAMMA}/events/keyset?{query}")
        for event in payload.get("events", []):
            if not isinstance(event, dict):
                continue
            title = event.get("title") or ""
            if event.get("closed"):
                continue
            end = parse_utc(event.get("endDate"))
            if end is None or not now < end <= now + MAX_HORIZON:
                continue
            if float(event.get("liquidity") or 0) < min_liquidity:
                continue
            tradable = []
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                tokens = _list(market.get("clobTokenIds"))
                outcomes = _list(market.get("outcomes"))
                question = market.get("question") or ""
                if not supported_policy_market(title, question):
                    continue
                if len(tokens) != 2 or [str(x).lower() for x in outcomes] != ["yes", "no"]:
                    continue
                if market.get("closed") or not market.get("active") or not market.get("acceptingOrders") or not market.get("enableOrderBook"):
                    continue
                market_end = parse_utc(market.get("endDate"))
                if market_end is None or not now < market_end <= now + MAX_HORIZON:
                    continue
                tradable.append({
                    "id": str(market["id"]), "event_id": str(event["id"]),
                    "event_title": title, "event_slug": event.get("slug") or "",
                    "question": question,
                    "description": market.get("description") or event.get("description") or "",
                    "end_at": market.get("endDate") or event.get("endDate"),
                    "yes_token": str(tokens[0]), "no_token": str(tokens[1]),
                    "liquidity": float(market.get("liquidity") or event.get("liquidity") or 0),
                    "volume_24h": float(market.get("volume24hr") or event.get("volume24hr") or 0),
                    "min_order_size": float(market.get("orderMinSize") or 5),
                    "tick_size": float(market.get("orderPriceMinTickSize") or 0.01),
                    "fee_rate": float((market.get("feeSchedule") or {}).get("rate") or 0),
                })
            if tradable:
                tradable.sort(key=lambda m: (-m["volume_24h"], -m["liquidity"]))
                found[str(event["id"])] = {"event": event, "markets": tradable[:6]}
    ranked = sorted(found.values(), key=lambda x: (
        parse_utc(x["event"].get("endDate")) or datetime.max.replace(tzinfo=timezone.utc),
        -float(x["event"].get("volume24hr") or 0)))
    return [m for group in ranked for m in group["markets"]]


def _short_market(event: dict, market: dict, kind: str, end_at: str,
                  labels: tuple[str, str]) -> dict | None:
    if not isinstance(event, dict) or not isinstance(market, dict):
        return None
    tokens, outcomes = _list(market.get("clobTokenIds")), _list(market.get("outcomes"))
    if (len(tokens) != 2 or tuple(outcomes) != labels or
            market.get("closed") or not market.get("active") or
            not market.get("acceptingOrders") or not market.get("enableOrderBook")):
        return None
    if not all(str(token).isdigit() for token in tokens):
        return None
    if parse_utc(end_at) is None or parse_utc(end_at) <= datetime.now(timezone.utc):
        return None
    return {
        "id": str(market["id"]), "event_id": str(event["id"]),
        "event_title": event["title"], "event_slug": event["slug"],
        "question": market["question"], "description": market.get("description") or "",
        "end_at": end_at, "yes_token": str(tokens[0]), "no_token": str(tokens[1]),
        "liquidity": float(market.get("liquidity") or event.get("liquidity") or 0),
        "volume_24h": float(market.get("volume24hr") or event.get("volume24hr") or 0),
        "min_order_size": float(market.get("orderMinSize") or 5),
        "tick_size": float(market.get("orderPriceMinTickSize") or .01),
        "fee_rate": float((market.get("feeSchedule") or {}).get("rate") or 0),
        "category": kind, "yes_label": labels[0], "no_label": labels[1],
    }


CHAINLINK_BTC_TWAP = "https://data.chain.link/streams/btc-usd-twap-60s-streams"


def discover_btc_5m(now: datetime | None = None) -> list[dict]:
    """Find the current/next BTC 5-minute markets by their exact UTC window."""
    now = now or datetime.now(timezone.utc)
    window = int(now.timestamp() // 300 * 300)
    selected = []
    for start in (window, window + 300):
        slug = f"btc-updown-5m-{start}"
        try:
            event = fetch_json(f"{GAMMA}/events/slug/{slug}")
            if (not isinstance(event, dict) or event.get("slug") != slug or
                    event.get("closed") or len(event.get("markets") or []) != 1):
                continue
            market = event["markets"][0]
            end = datetime.fromtimestamp(start + 300, timezone.utc)
            description = market.get("description") or ""
            config = market.get("cryptoMarketConfig") or {}
            if (market.get("resolutionSource") != CHAINLINK_BTC_TWAP or
                    parse_utc(market.get("endDate")) != end or
                    config.get("id") != "btc-5m-twap-60" or
                    config.get("twapEnabled") is not True or
                    config.get("twapLookbackSeconds") != 60 or
                    "time-weighted average price" not in description or
                    "greater than or equal" not in description):
                continue
            item = _short_market(event, market, "crypto5m", end.isoformat(), ("Up", "Down"))
            if item and item["liquidity"] >= 5000:
                item["source_ref"] = f"BTCUSD:{start}"
                selected.append(item)
        except (KeyError, TypeError, ValueError, RuntimeError):
            continue
    return selected


def discover_hourly_crypto(now: datetime | None = None) -> list[dict]:
    """Only Binance one-hour candles with an exact Up/Down resolution rule."""
    now = now or datetime.now(timezone.utc)
    eastern_zone = ZoneInfo("America/New_York")
    selected: list[dict] = []
    for offset in (0, 1):
        hour = (now + timedelta(hours=offset)).astimezone(eastern_zone).replace(
            minute=0, second=0, microsecond=0)
        clock = f"{hour.hour % 12 or 12}{'am' if hour.hour < 12 else 'pm'}"
        date = f"{hour.strftime('%B').lower()}-{hour.day}-{hour.year}"
        for name, symbol in (("bitcoin", "BTC"), ("ethereum", "ETH")):
            slug = f"{name}-up-or-down-{date}-{clock}-et"
            try:
                event = fetch_json(f"{GAMMA}/events/slug/{slug}")
                if not isinstance(event, dict) or event.get("slug") != slug or event.get("closed"):
                    continue
                market = next((m for m in event.get("markets", [])
                               if isinstance(m, dict) and m.get("question") == event.get("title")), None)
                if market is None:
                    continue
                end = parse_utc(market.get("endDate"))
                if (end is None or abs((end - hour.astimezone(timezone.utc)).total_seconds() - 3600) > 1
                        or f"{symbol}/USDT 1 hour candle" not in (market.get("description") or "")
                        or "greater than or equal" not in (market.get("description") or "")
                        or market.get("resolutionSource") !=
                        f"https://www.binance.com/en/trade/{symbol}_USDT"):
                    continue
                item = _short_market(event, market, "crypto", market["endDate"], ("Up", "Down"))
                if item and item["liquidity"] >= 1000:
                    item["source_ref"] = f"{symbol}USDT:{int(hour.timestamp())}"
                    selected.append(item)
            except (KeyError, TypeError, ValueError, RuntimeError):
                continue
    return selected


def discover_daily_mlb(now: datetime | None = None) -> list[dict]:
    """Select only pre-game MLB moneyline markets mapped to official schedule IDs."""
    now = now or datetime.now(timezone.utc)
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    selected: list[dict] = []
    aliases = {"was": "wsh", "lan": "lad", "nya": "nyy", "sln": "stl",
               "cha": "chw", "chn": "chc", "sdn": "sd", "sfn": "sf", "tba": "tb"}
    for day_offset in (0, 1):
        day = (eastern + timedelta(days=day_offset)).date().isoformat()
        try:
            schedule = fetch_json("https://statsapi.mlb.com/api/v1/schedule?" +
                                  urlencode({"sportId": 1, "date": day, "hydrate": "team,probablePitcher"}))
        except (ValueError, RuntimeError):
            continue
        for date in schedule.get("dates", []):
            for game in date.get("games", []):
                try:
                    start = parse_utc(game["gameDate"])
                    if start is None or not timedelta(minutes=25) < start - now < timedelta(hours=30):
                        continue
                    away = game["teams"]["away"]["team"]
                    home = game["teams"]["home"]["team"]
                    away_code = aliases.get(away["fileCode"], away["fileCode"])
                    home_code = aliases.get(home["fileCode"], home["fileCode"])
                    slug = f"mlb-{away_code}-{home_code}-{day}"
                    event = fetch_json(f"{GAMMA}/events/slug/{slug}")
                    if not isinstance(event, dict) or event.get("slug") != slug or event.get("closed"):
                        continue
                    market = next((m for m in event.get("markets", []) if isinstance(m, dict)
                                   and m.get("question") == event.get("title")), None)
                    if market is None or market.get("resolutionSource") != "https://www.mlb.com/":
                        continue
                    if parse_utc(market.get("gameStartTime")) != start:
                        continue
                    description = market.get("description") or ""
                    labels = (away["name"], home["name"])
                    if not all(f'"{label}"' in description for label in labels):
                        continue
                    item = _short_market(event, market, "daily", start.isoformat(), labels)
                    if item and item["liquidity"] >= 5000:
                        item["source_ref"] = str(game["gamePk"])
                        selected.append(item)
                except (KeyError, TypeError, ValueError, RuntimeError):
                    continue
    selected.sort(key=lambda item: (-item["volume_24h"], item["end_at"]))
    return selected[:3]


def order_book(token_id: str) -> dict:
    if not token_id.isdigit():
        raise ValueError("Invalid token ID")
    raw = fetch_json(f"{CLOB}/book?{urlencode({'token_id': token_id})}",
                     user_agent="PolycastPaperResearch/1.0")
    if str(raw.get("asset_id")) != token_id:
        raise RuntimeError("Order book token mismatch")
    return raw


def book_baseline(book: dict, max_age_seconds: int) -> tuple[str, float, float] | None:
    """Return a fresh YES bid/ask snapshot for a market-price benchmark."""
    try:
        book_time = datetime.fromtimestamp(int(book["timestamp"]) / 1000, timezone.utc)
        bids = [float(level["price"]) for level in book.get("bids", [])
                if float(level["size"]) > 0 and 0 < float(level["price"]) < 1]
        asks = [float(level["price"]) for level in book.get("asks", [])
                if float(level["size"]) > 0 and 0 < float(level["price"]) < 1]
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    age = (datetime.now(timezone.utc) - book_time).total_seconds()
    if age < -5 or age > max_age_seconds or not bids or not asks:
        return None
    best_bid, best_ask = max(bids), min(asks)
    if best_bid > best_ask:
        return None
    return book_time.isoformat(timespec="seconds"), best_bid, best_ask


def live_market(market_id: str) -> dict:
    if not market_id.isdigit():
        raise ValueError("Invalid market ID")
    return fetch_json(f"{GAMMA}/markets/{market_id}")


def clob_market_info(condition_id: str) -> dict:
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition_id):
        raise ValueError("Invalid CLOB condition ID")
    raw = fetch_json(f"{CLOB}/clob-markets/{condition_id}")
    if raw.get("c") != condition_id:
        raise ValueError("CLOB condition mismatch")
    return raw


def executable_ask(book: dict, shares: float) -> tuple[float, float] | None:
    """Return volume-weighted ask and worst ask for requested shares."""
    fills = match_levels(book, shares, "asks")
    return (fill_totals(fills, shares, 0)[1], fills[-1]["price"]) if fills else None


def executable_bid(book: dict, shares: float) -> tuple[float, float] | None:
    """Return volume-weighted proceeds and lowest bid for liquidating shares."""
    fills = match_levels(book, shares, "bids")
    return (fill_totals(fills, shares, 0)[1], fills[-1]["price"]) if fills else None
