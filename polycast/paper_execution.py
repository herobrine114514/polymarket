"""Delayed public snapshot FOK approximation; never a real execution receipt."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import time

from .config import Config
from .book_drift import assess_book_drift, book_top
from .execution import FEE_METHOD, fill_totals, match_levels
from .market import clob_market_info, order_book, parse_utc
from .store import digest_text

PAPER_EXECUTION_METHOD = "delayed_snapshot_fok_v2_forecast_basis"


class PaperExecutionError(ValueError):
    def __init__(self, message: str, audit: dict):
        super().__init__(message)
        self.audit = audit


def execution_terms(saved: dict, live: dict, info: dict) -> dict:
    """Validate public execution metadata; unsupported fee/protocols fail closed."""
    condition = live.get("conditionId")
    if info.get("c") != condition or info.get("ao") is not True:
        raise ValueError("CLOB condition or accepting-orders state changed")
    if info.get("v") != "v1":
        raise ValueError("Paper token execution supports verified v1 markets only")
    tokens = info.get("t")
    if not isinstance(tokens, list) or len(tokens) != 2:
        raise ValueError("CLOB binary token mapping missing")
    expected = {saved["yes_token"]: saved["yes_label"], saved["no_token"]: saved["no_label"]}
    if {str(t.get("t")): t.get("o") for t in tokens} != expected:
        raise ValueError("CLOB token or outcome mapping changed")
    minimum, tick = float(info["mos"]), float(info["mts"])
    if (not all(math.isfinite(x) and x > 0 for x in (minimum, tick)) or
            minimum != float(saved["min_order_size"]) or tick != float(saved["tick_size"]) or
            minimum != float(live["orderMinSize"]) or tick != float(live["orderPriceMinTickSize"])):
        raise ValueError("CLOB minimum size or tick changed")
    fee = info.get("fd")
    if (not isinstance(fee, dict) or type(fee.get("r")) not in (int,float) or
            type(fee.get("e")) not in (int,float) or fee.get("e") != 1):
        raise ValueError("Missing or unsupported CLOB fee model")
    rate = float(fee["r"])
    if not math.isfinite(rate) or not 0 <= rate < 1 or rate != float(saved["fee_rate"]):
        raise ValueError("CLOB fee rate changed")
    if fee.get("to") is not True:
        raise ValueError("Unsupported CLOB fee scope")
    taker_flag = info.get("itode")
    if taker_flag is not None and type(taker_flag) is not bool:
        raise ValueError("Invalid taker-delay flag")
    seconds = live.get("secondsDelay")
    seconds = 0 if seconds is None else seconds
    if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0 <= seconds <= 120:
        raise ValueError("Unsupported market seconds delay")
    # A conservative minimum also resamples markets that omit the taker flag.
    # An absent flag is NOT claimed to prove a platform delay of zero.
    hold_ms = max(250, math.ceil(seconds * 1000))
    return {"condition_id": condition, "protocol": "v1", "fee_rate": rate,
            "fee_exponent": 1, "minimum_size": minimum, "tick_size": tick,
            "taker_delay_flag": taker_flag, "market_seconds_delay": seconds,
            "sample_hold_ms": hold_ms, "minimum_snapshot_hold_ms": 250}


def sample_fok_execution(saved: dict, frozen: dict, config: Config, live: dict) -> tuple[dict,dict]:
    """Keep original side, shares and price cap; resample after validated delay."""
    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    audit = {"method": PAPER_EXECUTION_METHOD, "started_at": started.isoformat(),
             "frozen_quote_sha256": digest_text(frozen), "frozen_limit_price": frozen["worst_ask"],
             "shares": frozen["shares"], "side": frozen["side"], "external_orders_submitted": 0,
             "scope": "Delayed public aggregated book approximation; no queue or onchain fill proof"}
    try:
        terms = execution_terms(saved,live,clob_market_info(live.get("conditionId", "")))
        audit["terms"] = terms
        hold_clock = time.monotonic()
        while (time.monotonic() - hold_clock) * 1000 < terms["sample_hold_ms"]:
            time.sleep(max(0,terms["sample_hold_ms"] / 1000 - (time.monotonic() - hold_clock)))
        audit["hold_elapsed_ms"] = (time.monotonic() - hold_clock) * 1000
        deadline = parse_utc(saved["end_at"])
        if deadline is None or datetime.now(timezone.utc) >= deadline:
            raise ValueError("Market entry cutoff passed during execution delay")
        # Refresh after the delay, with NO SQLite transaction held by the caller.
        token = saved["yes_token"] if frozen["side"] == "YES" else saved["no_token"]
        book = order_book(token)
        received = datetime.now(timezone.utc)
        audit.update(book_received_at=received.isoformat(),book=book)
        if book.get("market") != terms["condition_id"] or book.get("asset_id") != token:
            raise ValueError("Execution book identity mismatch")
        if float(book["min_order_size"]) != terms["minimum_size"] or float(book["tick_size"]) != terms["tick_size"]:
            raise ValueError("Execution book trading parameters changed")
        book_time = datetime.fromtimestamp(int(book["timestamp"]) / 1000,timezone.utc)
        age = (received - book_time).total_seconds()
        if not -5 <= age <= min(5,config.quote_max_age_seconds):
            raise ValueError("Post-delay execution book is stale or in the future")
        cap = float(frozen["worst_ask"])
        bounded = {"asks": [level for level in book.get("asks",[]) if float(level["price"]) <= cap + 1e-12]}
        fills = match_levels(bounded,float(frozen["shares"]),"asks")
        if fills is None:
            raise ValueError("FOK depth disappeared at frozen limit after delay")
        gross,average,fees = fill_totals(fills,float(frozen["shares"]),terms["fee_rate"])
        quote = {**frozen,"avg_ask":average,"worst_ask":fills[-1]["price"],"fills":fills,
                 "fee_method":FEE_METHOD,"fee_per_share":fees / frozen["shares"],
                 "total_cost":gross + fees,"book_timestamp":book_time.isoformat(timespec="microseconds"),
                 "captured_at":received.isoformat(timespec="microseconds")}
        if saved.get('category') == 'crypto5m':
            top = book_top(book)
            quote.update(book_bid=top[0] if top else None, book_ask=top[1] if top else None)
            drift = assess_book_drift(frozen.get('forecast_book_basis'), quote, config, now=received)
            audit['forecast_book_check'] = drift
            if not drift['allowed']:
                raise ValueError('BTC5m forecast basis invalidated after delay: ' + drift['reason'])
            quote['forecast_book_check'] = drift
        quote["expected_edge"] = frozen["expected_edge"] + frozen["avg_ask"] + frozen["fee_per_share"] - average - quote["fee_per_share"]
        if "expected_roi" in frozen:
            quote["expected_roi"] = quote["expected_edge"] / (average + quote["fee_per_share"])
        if quote["total_cost"] > frozen["total_cost"] + 1e-8:
            raise ValueError("Post-delay fill exceeds reserved maximum cost")
        audit["execution_quote_sha256"] = digest_text(quote)
        return quote,audit
    except (KeyError,TypeError,ValueError,OverflowError,RuntimeError) as exc:
        audit["failure_reason"] = str(exc)[:250]
        raise PaperExecutionError(str(exc)[:250],audit) from exc
    finally:
        audit["completed_at"] = datetime.now(timezone.utc).isoformat()
        audit["total_elapsed_ms"] = (time.monotonic() - clock) * 1000
