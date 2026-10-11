"""Compare two paper fill calculations on identical frozen public books.

Run in .polycast/oss-paper-venv (polymarket-paper-trader==0.4.2).
No account is initialized, no database is written, and no model or order API
is called. --live adds current BTC 5m public market and book snapshots.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm_trader.models import OrderBook, OrderBookLevel
from pm_trader.orderbook import simulate_buy_fill, simulate_sell_fill
from polycast.config import Config
from polycast.execution import fill_totals, match_levels
from polycast.market import discover_btc_5m, executable_bid, live_market, order_book, parse_utc
from polycast.paper import _quote


def compare(book: dict, shares: float, rate: float) -> dict:
    parsed = OrderBook(
        bids=[OrderBookLevel(price=float(x["price"]), size=float(x["size"])) for x in book["bids"]],
        asks=[OrderBookLevel(price=float(x["price"]), size=float(x["size"])) for x in book["asks"]])
    market = {"yes_token": "frozen", "no_token": "unused", "fee_rate": rate}
    with patch("polycast.paper.order_book", return_value=book):
        local = _quote(market, "YES", shares, Config())
    # The local engine sizes by shares; convert its gross notional to the
    # candidate's USD input. This compares the same fill, not two fetch times.
    budget = shares * local["avg_ask"] if local else sum(x.price * x.size for x in parsed.asks) + 1
    candidate = simulate_buy_fill(parsed, budget, 0, "fok", fee_rate=rate)
    fill_match = (local is None and not candidate.filled) or (
        local is not None and candidate.filled and
        math.isclose(shares, candidate.total_shares, rel_tol=0, abs_tol=1e-8) and
        math.isclose(local["avg_ask"], candidate.avg_price, rel_tol=0, abs_tol=1e-8))
    buy = {"filled_local": local is not None, "filled_candidate": candidate.filled,
           "fill_matches": bool(fill_match), "shares": shares,
           "avg_local": local["avg_ask"] if local else None,
           "avg_candidate": candidate.avg_price if candidate.filled else None,
           "fee_local": shares * local["fee_per_share"] if local else None,
           "fee_candidate": candidate.fee if candidate.filled else None,
           "fee_delta_local_minus_candidate": round(shares * local["fee_per_share"] - candidate.fee, 8)
           if local and candidate.filled else None,
           "candidate_fills": [{"price": f.price, "shares": f.shares} for f in candidate.fills]}
    local_bid = executable_bid(book, shares)
    candidate_sell = simulate_sell_fill(parsed, shares, 0, "fok", fee_rate=rate)
    sell_match = (local_bid is None and not candidate_sell.filled) or (
        local_bid is not None and candidate_sell.filled and
        math.isclose(local_bid[0], candidate_sell.avg_price, rel_tol=0, abs_tol=1e-8))
    local_sell_fee = (fill_totals(match_levels(book, shares, "bids"), shares, rate)[2]
                      if local_bid else None)
    sell = {"filled_local": local_bid is not None, "filled_candidate": candidate_sell.filled,
            "fill_matches": bool(sell_match),
            "avg_local": local_bid[0] if local_bid else None,
            "avg_candidate": candidate_sell.avg_price if candidate_sell.filled else None,
            "fee_local": local_sell_fee,
            "fee_candidate": candidate_sell.fee if candidate_sell.filled else None}
    return {"buy": buy, "sell": sell}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".polycast/oss-paper-comparison.json"))
    args = parser.parse_args()
    package_version = version("polymarket-paper-trader")
    if package_version != "0.4.2":
        raise SystemExit("Use the pinned isolated package version 0.4.2")
    timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    fixtures = [
        ("single_level", {"timestamp": timestamp, "asks": [{"price": ".5", "size": "10"}],
                          "bids": [{"price": ".4", "size": "10"}]}, 5, .07),
        ("multiple_levels", {"timestamp": timestamp, "asks": [{"price": ".8", "size": "8"},
                            {"price": ".2", "size": "2"}], "bids": [{"price": ".1", "size": "8"},
                            {"price": ".7", "size": "2"}]}, 5, .07),
        ("insufficient_fok", {"timestamp": timestamp, "asks": [{"price": ".5", "size": "2"}],
                              "bids": [{"price": ".4", "size": "2"}]}, 5, .07),
    ]
    results = [{"name": name, "input": {"book": book, "shares": shares, "fee_rate": rate},
                **compare(book, shares, rate)} for name, book, shares, rate in fixtures]
    if args.live:
        now = datetime.now(timezone.utc)
        markets = discover_btc_5m(now)
        market = next(m for m in markets if
                      datetime.fromtimestamp(int(m["source_ref"].split(":")[1]), timezone.utc) <= now and
                      parse_utc(m["end_at"]) > now + timedelta(seconds=30))
        remote = live_market(market["id"])
        schedule = remote.get("feeSchedule")
        if (not isinstance(schedule, dict) or schedule.get("exponent") != 1 or
                not math.isfinite(float(schedule["rate"])) or
                float(schedule["rate"]) != market["fee_rate"]):
            raise SystemExit("Published fee schedule unsupported or changed")
        for side, token in (("YES", market["yes_token"]), ("NO", market["no_token"])):
            book = order_book(token)
            book_at = datetime.fromtimestamp(int(book["timestamp"]) / 1000, timezone.utc)
            if not -5 <= (datetime.now(timezone.utc) - book_at).total_seconds() <= 10:
                raise SystemExit("Public book is stale")
            if any(not 0 < float(x["price"]) < 1 or not math.isfinite(float(x["size"])) or
                   float(x["size"]) <= 0 for key in ("asks", "bids") for x in book[key]):
                raise SystemExit("Book contains unsupported levels")
            shares = max(5, market["min_order_size"])
            results.append({"name": "live_" + side, "market_id": market["id"],
                            "event_slug": market["event_slug"],
                            "input": {"book": book, "shares": shares, "fee_rate": market["fee_rate"],
                                      "published_fee_schedule": schedule},
                            **compare(book, shares, market["fee_rate"])})
    path = Path(inspect.getfile(simulate_buy_fill))
    result = {"recorded_at": datetime.now(timezone.utc).isoformat(),
              "package": "polymarket-paper-trader", "version": package_version,
              "candidate_orderbook_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "scope": "Identical frozen public book; fills and fees only, not model quality or live execution",
              "model_calls": 0, "order_api_calls": 0, "database_writes": 0,
              "fill_matches_all": all(r[side]["fill_matches"] for r in results for side in ("buy", "sell")),
              "cases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False))
    for case in results:
        print(json.dumps({"case": case["name"], "market_id": case.get("market_id"),
                          "buy": case["buy"], "sell": case["sell"]}, ensure_ascii=False))
    if not result["fill_matches_all"]:
        raise SystemExit("Fill calculation disagreement requires investigation")


if __name__ == "__main__":
    main()
