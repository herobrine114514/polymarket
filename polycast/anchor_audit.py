"""Read-only audit of BTC five-minute relay boundaries against Gamma outcomes.

The RTDS values are observable relay proxies, not signed settlement reports or
an independently verified price-to-beat. Include unresolved local markets so
the comparison is not selected on the existing reconciliation rule.
"""
from __future__ import annotations

import bisect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .market import GAMMA, CHAINLINK_BTC_TWAP, _list, parse_utc
from .net import fetch_json
from .twap_records import decode_twap_record


def _relay_points(path: Path) -> tuple[list[int], dict[int, tuple[Decimal, int | None]], int]:
    points: dict[int, tuple[Decimal, int | None]] = {}
    conflicts = 0
    if not path.is_file():
        return [], points, conflicts
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
                point = decode_twap_record(item, require_receipt=False)
                stamp, value, received = (point["timestamp_ms"], point["price"],
                                          point["received_at_ms"])
                if stamp <= 0 or not value.is_finite() or value <= 0 or (received is not None and received < stamp - 5000):
                    continue
                previous = points.get(stamp)
                if previous is not None and previous[0] != value:
                    conflicts += 1
                if previous is None or (received is not None and
                                        (previous[1] is None or received < previous[1])):
                    points[stamp] = (value, received)
            except (KeyError, ValueError, TypeError, InvalidOperation, json.JSONDecodeError):
                continue
    return sorted(points), points, conflicts


def _nearest(stamps: list[int], points: dict[int, tuple[Decimal, int | None]],
             boundary_ms: int) -> dict | None:
    pos = bisect.bisect_left(stamps, boundary_ms)
    candidates = stamps[max(0, pos - 1):min(len(stamps), pos + 1)]
    if not candidates:
        return None
    stamp = min(candidates, key=lambda candidate: (abs(candidate - boundary_ms), candidate))
    if abs(stamp - boundary_ms) > 3000:
        return None
    value, received = points[stamp]
    return {"timestamp_ms": stamp, "value": str(value),
            "boundary_offset_seconds": (stamp - boundary_ms) / 1000,
            "relay_lag_seconds": (received - stamp) / 1000 if received is not None else None,
            "received_at_ms": received}


def _gamma_side(market: dict, event: dict, now: datetime) -> str | None:
    remote = next((candidate for candidate in event.get("markets", [])
                   if isinstance(candidate, dict) and str(candidate.get("id")) == market["id"]), None)
    if (str(event.get("id")) != market["event_id"] or remote is None or
            not event.get("closed") or not remote.get("closed") or
            (remote.get("description") or "") != market["description"] or
            _list(remote.get("outcomes")) != [market["yes_label"], market["no_label"]] or
            _list(remote.get("clobTokenIds")) != [market["yes_token"], market["no_token"]] or
            parse_utc(remote.get("endDate")) != parse_utc(market["end_at"]) or
            remote.get("resolutionSource") != CHAINLINK_BTC_TWAP or
            market["source_ref"] != market["event_slug"].replace("btc-updown-5m-", "BTCUSD:") or
            (remote.get("cryptoMarketConfig") or {}).get("id") != "btc-5m-twap-60" or
            (remote.get("cryptoMarketConfig") or {}).get("twapLookbackSeconds") != 60):
        return None
    close_time = parse_utc(remote.get("closedTime") or event.get("closedTime"))
    if close_time is None or not parse_utc(market["end_at"]) < close_time <= now - timedelta(minutes=2):
        return None
    prices = _list(remote.get("outcomePrices"))
    return "YES" if prices == ["1", "0"] else "NO" if prices == ["0", "1"] else None


def audit_btc5m_anchors(conn, path: Path, *, limit: int = 50,
                        now: datetime | None = None, fetcher=fetch_json) -> dict:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    now = now or datetime.now(timezone.utc)
    stamps, points, conflicts = _relay_points(path)
    markets = [dict(row) for row in conn.execute("""SELECT m.*,r.winning_side AS local_side
        FROM markets m LEFT JOIN resolutions r ON r.market_id=m.id
        WHERE m.category='crypto5m' AND m.end_at<?
        ORDER BY m.end_at DESC LIMIT ?""",
        ((now - timedelta(minutes=2)).isoformat(), limit))]
    gaps = [(right - left, left, right) for left, right in zip(stamps, stamps[1:])
            if right - left > 30000]
    longest_gaps = sorted(gaps, reverse=True)[:5]
    discovered_starts = sorted({int(market["source_ref"].split(":")[1]) for market in markets})
    possible_windows = ((discovered_starts[-1] - discovered_starts[0]) // 300 + 1
                        if discovered_starts else 0)
    results = []
    totals = {"gamma_final": 0, "relay_matched": 0, "relay_disagreed": 0,
              "relay_missing": 0, "gamma_unverified": 0, "fetch_errors": 0,
              "first_prediction_markets": 0,
              "opening_available_at_first_prediction": 0}
    for market in markets:
        row = {"market_id": market["id"], "event_slug": market["event_slug"],
               "start_at": None, "end_at": market["end_at"],
               "local_resolution": market["local_side"], "gamma_side": None,
               "relay_side": None, "opening": None, "closing": None,
               "first_prediction_at": None, "opening_available_at_first_prediction": None,
               "status": "gamma_unverified"}
        try:
            start = datetime.fromtimestamp(int(market["source_ref"].split(":")[1]), timezone.utc)
            end = parse_utc(market["end_at"])
            if end is None or end != start + timedelta(minutes=5):
                raise ValueError("invalid market window")
            row["start_at"] = start.isoformat()
            opening = _nearest(stamps, points, int(start.timestamp() * 1000))
            closing = _nearest(stamps, points, int(end.timestamp() * 1000))
            row["opening"], row["closing"] = opening, closing
            if opening and closing and closing["timestamp_ms"] > opening["timestamp_ms"]:
                row["relay_side"] = ("YES" if Decimal(closing["value"]) >= Decimal(opening["value"])
                                     else "NO")
            first = conn.execute("""SELECT MIN(as_of) FROM predictions
                WHERE market_id=? AND abstain=0""", (market["id"],)).fetchone()[0]
            row["first_prediction_at"] = first
            totals["first_prediction_markets"] += int(first is not None)
            if first and opening and opening["received_at_ms"] is not None:
                row["opening_available_at_first_prediction"] = (
                    opening["received_at_ms"] <= int(parse_utc(first).timestamp() * 1000))
                totals["opening_available_at_first_prediction"] += int(
                    row["opening_available_at_first_prediction"])
            event = fetcher(f"{GAMMA}/events/slug/{market['event_slug']}")
            row["gamma_side"] = _gamma_side(market, event, now)
            if row["gamma_side"] is None:
                totals["gamma_unverified"] += 1
            else:
                totals["gamma_final"] += 1
                if row["relay_side"] is None:
                    row["status"] = "relay_missing"
                    totals["relay_missing"] += 1
                elif row["relay_side"] == row["gamma_side"]:
                    row["status"] = "relay_matched"
                    totals["relay_matched"] += 1
                else:
                    row["status"] = "relay_disagreed"
                    totals["relay_disagreed"] += 1
        except (KeyError, ValueError, TypeError, AttributeError, RuntimeError, OSError) as exc:
            row["status"] = "fetch_or_metadata_error"
            row["error_type"] = type(exc).__name__
            totals["fetch_errors"] += 1
        results.append(row)
    return {"method": "Locally discovered BTC 5m markets only; Gamma final versus nearest Polymarket RTDS 60s TWAP samples within 3s of each boundary; relay is not independently verified price-to-beat",
            "selected_markets": len(markets),
            "possible_five_minute_windows_in_selected_span": possible_windows,
            "unobserved_window_slots_in_selected_span": max(0, possible_windows - len(discovered_starts)),
            "relay_points": len(stamps),
            "conflicting_relay_timestamps": conflicts,
            "relay_gaps_over_30_seconds": len(gaps),
            "longest_relay_gaps": [{"seconds": seconds / 1000,
                "from_at": datetime.fromtimestamp(left / 1000, timezone.utc).isoformat(),
                "to_at": datetime.fromtimestamp(right / 1000, timezone.utc).isoformat()}
                for seconds, left, right in longest_gaps],
            "totals": totals, "markets": results}
