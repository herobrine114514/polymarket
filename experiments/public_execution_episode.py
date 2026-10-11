"""Bounded public GET capture and deterministic fixed-share FOK snapshot replay.

No wallet, model, production database, paper order or order transport. A locally
sealed intent precedes every later request. This is an execution diagnostic, not
an exchange receipt, prospective forecast registration or profitability proof.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from polycast.public_clock import observe_public_clock

VERSION = "public-fixed-share-delayed-book-v1"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
TWAP_SOURCE = "https://data.chain.link/streams/btc-usd-twap-60s-streams"
MAX_BYTES = 8 * 1024 ** 2
MAX_REQUESTS = 12
TICKS = {Decimal(x) for x in ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001")}
LIMITATIONS = [
    "Public aggregated snapshots do not prove maker queues or actual exchange fills.",
    "Fees are per-level estimates; maker-match splitting and operator rounding are unknown.",
    "Order-book documentation says minimum shares; market-details documentation says minimum USDC. Gamma unit is unresolved.",
    "Protocol V2 is rejected by this replay; no production protocol support is implied.",
    "No model probability, official outcome or full-service cost was supplied; no profit score.",
    "Clock is a public HTTPS interval estimate, not signed time or system clock correction.",
]


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def number(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Invalid " + name)
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid " + name) from exc
    if not result.is_finite():
        raise ValueError("Nonfinite " + name)
    return result


def integer(value: object, name: str, *, positive: bool = True) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError("Invalid " + name)
    return value


def array(value: object) -> list:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("Expected array")
    return value


def allowed_url(url: str) -> bool:
    return bool(isinstance(url, str) and (
        url in (CLOB + "/time", "https://api.exchange.coinbase.com/time") or
        re.fullmatch(re.escape(GAMMA) + r"/markets/[0-9]+", url) or
        re.fullmatch(re.escape(GAMMA) + r"/events/[0-9]+", url) or
        re.fullmatch(re.escape(CLOB) + r"/clob-markets/0x[0-9a-fA-F]{64}", url) or
        re.fullmatch(re.escape(CLOB) + r"/book\?token_id=[0-9]+", url)))


def clock_utc(clock: dict, local_ms: int) -> tuple[Decimal, Decimal]:
    checked = integer(clock.get("checked_at_local_ms"), "clock receipt")
    offset = number(clock.get("offset_ms"), "clock offset")
    uncertainty = number(clock.get("uncertainty_ms"), "clock uncertainty")
    if (clock.get("status") != "venues_agree" or not 0 <= local_ms - checked <= 120000 or
            abs(offset) > 10000 or not 0 <= uncertainty <= 750 or
            not re.fullmatch(r"[0-9a-f]{64}", str(clock.get("clock_id")))):
        raise ValueError("Fresh verified public clock required")
    return Decimal(local_ms) + offset, uncertainty


def no_duplicate_keys(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def receipt_body(receipt: dict, url: str, *, object_required: bool = True):
    if (receipt.get("method") != "GET" or receipt.get("authenticated") is not False or
            receipt.get("url") != url or not allowed_url(url) or receipt.get("http_status") != 200):
        raise ValueError("Unverified public GET receipt")
    raw = receipt.get("raw_text")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES or sha(raw) != receipt.get("raw_sha256"):
        raise ValueError("Public response hash/size mismatch")
    sent = integer(receipt.get("sent_local_ms"), "request sent")
    received = integer(receipt.get("received_local_ms"), "request received")
    mono_sent = integer(receipt.get("sent_monotonic_ns"), "request monotonic sent")
    mono_received = integer(receipt.get("received_monotonic_ns"), "request monotonic received")
    elapsed = (mono_received - mono_sent) / 1e6
    if not 0 <= elapsed <= 5000 or abs(received - sent - elapsed) > 100:
        raise ValueError("Request clock step or excessive transport latency")
    body = json.loads(raw, object_pairs_hook=no_duplicate_keys,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    if object_required and not isinstance(body, dict):
        raise ValueError("Public JSON object required")
    return body


def verify_clock_receipts(clock: dict, receipts: list[dict]) -> None:
    from polycast.public_clock import clock_intersection, URLS
    if len(receipts) != 2 or len(clock.get("samples", [])) != 2:
        raise ValueError("Original two-venue clock receipts required")
    for (venue, url), sample, receipt in zip(URLS.items(), clock["samples"], receipts):
        body = receipt_body(receipt, url, object_required=False)
        if venue == "polymarket":
            source = integer(body, "CLOB server clock") * 1000; precision = 1000
        else:
            epoch = body.get("epoch")
            if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
                raise ValueError("Invalid Coinbase server clock")
            source = epoch * 1000; precision = 1
        if (sample.get("venue") != venue or sample.get("source_url") != url or
                sample.get("server_ms") != source or sample.get("precision_ms") != precision or
                sample["sent_local_ms"] > receipt["sent_local_ms"] + 1 or
                sample["received_local_ms"] < receipt["received_local_ms"] - 1):
            raise ValueError("Clock estimate differs from original HTTP receipts")
    interval = clock_intersection(clock["samples"])
    if any(clock.get(key) != value for key, value in interval.items()):
        raise ValueError("Clock interval does not match samples")
    unhashed = {key: value for key, value in clock.items() if key != "clock_id"}
    if sha(json.dumps(unhashed, sort_keys=True)) != clock.get("clock_id"):
        raise ValueError("Clock identity does not match observation")


def terms(expected: dict, gamma: dict, clob: dict, event: dict) -> dict:
    """Explicitly bind version, contract, assets, fee scope, tick and delay."""
    from polycast.market import parse_utc
    market_id = str(expected["market_id"])
    integer(expected.get("start_at_ms"), "market start")
    integer(expected.get("end_at_ms"), "market end")
    condition = gamma.get("conditionId")
    if (gamma.get("version") != "v1" or clob.get("v") != "v1"):
        raise ValueError("Unsupported/missing market version; no token-ID fallback")
    if (str(gamma.get("id")) != market_id or not market_id.isdigit() or
            not isinstance(condition, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition) or
            clob.get("c") != condition):
        raise ValueError("Public market/condition identity mismatch")
    ids = array(gamma.get("clobTokenIds")); labels = array(gamma.get("outcomes"))
    assets = [expected["up_asset"], expected["down_asset"]]
    mappings = clob.get("t")
    if (ids != assets or len(set(assets)) != 2 or labels != ["Up", "Down"] or
            any(not isinstance(x, str) or not x.isdigit() for x in assets) or
            not isinstance(mappings, list) or len(mappings) != 2 or not all(isinstance(x, dict) for x in mappings) or
            {x.get("t"): x.get("o") for x in mappings} != dict(zip(assets, labels))):
        raise ValueError("Selected asset/outcome mapping mismatch")
    state = (gamma.get("active"), gamma.get("closed"), gamma.get("acceptingOrders"),
             gamma.get("enableOrderBook"), clob.get("ao"))
    if state != (True, False, True, True, True):
        raise ValueError("Market not accepting public book observations")
    config = gamma.get("cryptoMarketConfig") or {}
    if not isinstance(config, dict):
        raise ValueError("Invalid contract configuration")
    end = parse_utc(gamma.get("endDate"))
    if (end is None or int(end.timestamp() * 1000) != expected["end_at_ms"] or
            expected["end_at_ms"] - expected["start_at_ms"] != 300000 or
            gamma.get("description") != expected["rules"] or
            gamma.get("resolutionSource") != TWAP_SOURCE or
            config.get("id") != "btc-5m-twap-60" or config.get("twapEnabled") is not True or
            config.get("twapLookbackSeconds") != 60):
        raise ValueError("Contract target/rules/window mismatch")
    markets = event.get("markets")
    if (str(event.get("id")) != str(expected["event_id"]) or not isinstance(markets, list) or
            not all(isinstance(x, dict) for x in markets)):
        raise ValueError("Event identity not verified")
    selected = [x for x in markets if str(x.get("id")) == market_id]
    identity_fields = ("conditionId", "version", "clobTokenIds", "outcomes", "endDate", "description",
                       "resolutionSource", "cryptoMarketConfig", "orderMinSize", "orderPriceMinTickSize",
                       "feesEnabled", "feeSchedule", "active", "closed", "acceptingOrders", "enableOrderBook")
    if len(selected) != 1 or any(selected[0].get(key) != gamma.get(key) for key in identity_fields):
        raise ValueError("Event market mapping/terms conflict")
    tick = number(clob.get("mts"), "tick")
    minimum = number(clob.get("mos"), "CLOB minimum")
    if (tick not in TICKS or minimum <= 0 or
            tick != number(gamma.get("orderPriceMinTickSize"), "Gamma tick") or
            minimum != number(gamma.get("orderMinSize"), "Gamma minimum")):
        raise ValueError("Tick/minimum changed or missing")
    fee = clob.get("fd"); other = gamma.get("feeSchedule")
    if not isinstance(fee, dict) or not isinstance(other, dict):
        raise ValueError("Fee schedule missing")
    rate = number(fee.get("r"), "fee rate")
    if (not 0 <= rate < 1 or rate != number(other.get("rate"), "Gamma fee rate") or
            type(fee.get("e")) not in (int, float) or fee["e"] != 1 or
            type(other.get("exponent")) not in (int, float) or other["exponent"] != 1 or
            fee.get("to") is not True or other.get("takerOnly") is not True or
            type(gamma.get("feesEnabled")) is not bool or
            (gamma["feesEnabled"] is False and rate != 0)):
        raise ValueError("Unsupported/inconsistent fee schedule")
    flag = clob.get("itode"); delay = gamma.get("secondsDelay")
    if flag is not None and type(flag) is not bool:
        raise ValueError("Malformed taker delay")
    seconds = Decimal(0) if delay is None else number(delay, "sports delay")
    if not 0 <= seconds <= 120:
        raise ValueError("Unsupported matching delay")
    return {"market_id": market_id, "condition_id": condition, "version": "v1", "assets": assets,
            "fee_rate": str(rate), "fee_exponent": 1, "taker_only": True,
            "tick": str(tick), "minimum_from_clob": str(minimum),
            "minimum_from_gamma": str(minimum), "minimum_unit_status": "gamma_documentation_conflict",
            "book_minimum_unit": "shares_per_place_orders_documentation",
            "taker_delay_flag": flag, "seconds_delay": str(seconds),
            "seconds_delay_observed": delay is not None, "snapshot_hold_ms": max(250, int(seconds * 1000)),
            "target": "official-btc-5m-twap-60", "independent_reference_verified": False}


def book_quote(book: dict, receipt: dict, clock: dict, contract: dict, asset: str,
               shares: Decimal, *, limit: Decimal | None = None) -> dict:
    if book.get("market") != contract["condition_id"] or book.get("asset_id") != asset:
        raise ValueError("Book identity mismatch")
    tick = Decimal(contract["tick"]); minimum = Decimal(contract["minimum_from_clob"])
    if (number(book.get("tick_size"), "book tick") != tick or
            number(book.get("min_order_size"), "book minimum") != minimum):
        raise ValueError("Book constraints changed")
    if shares < minimum or shares != shares.quantize(Decimal("0.01")):
        raise ValueError("Below documented book-share minimum or wrong share precision")
    received_utc, uncertainty = clock_utc(clock, receipt["received_local_ms"])
    source = integer(book.get("timestamp"), "book timestamp")
    age_lower, age_upper = received_utc - uncertainty - source, received_utc + uncertainty - source
    if age_lower < 0 or age_upper > 5000:
        raise ValueError("Book timestamp freshness/future not proven within clock interval")
    depth = {}
    for side in ("bids", "asks"):
        rows = book.get(side)
        if not isinstance(rows, list):
            raise ValueError("Missing book depth")
        levels = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid depth row")
            price = number(row.get("price"), "depth price"); size = number(row.get("size"), "depth size")
            if not 0 < price < 1 or size < 0 or price % tick != 0 or price in levels:
                raise ValueError("Invalid/off-grid/duplicate depth level")
            if size > 0:
                levels[price] = size
        depth[side] = levels
    if not depth["bids"] or not depth["asks"] or max(depth["bids"]) >= min(depth["asks"]):
        raise ValueError("One-sided or crossed book")
    remaining = shares; fills = []; gross = Decimal(0); fees = Decimal(0)
    for price, available in sorted(depth["asks"].items()):
        if limit is not None and price > limit:
            break
        size = min(remaining, available)
        gross += price * size
        fee = (size * Decimal(contract["fee_rate"]) * price * (1 - price)).quantize(
            Decimal("0.00001"), rounding=ROUND_HALF_EVEN)
        fees += fee
        fills.append({"price": str(price), "shares": str(size), "estimated_fee": str(fee)})
        remaining -= size
        if remaining == 0:
            break
    if remaining != 0:
        raise ValueError("Full FOK depth unavailable within frozen limit")
    return {"asset_id": asset, "shares": str(shares), "limit_price": str(fills[-1]["price"]),
            "gross_cost": str(gross), "average_price": str(gross / shares),
            "estimated_fee": str(fees), "estimated_total_cost": str(gross + fees), "fills": fills,
            "fee_method": "per-level-decimal-half-even-5dp-estimate-v1",
            "book_timestamp_ms": source, "received_local_ms": receipt["received_local_ms"],
            "received_utc_interval_ms": [str(received_utc - uncertainty), str(received_utc + uncertainty)],
            "book_age_interval_ms": [str(age_lower), str(age_upper)],
            "gamma_notional_minimum_met": gross >= Decimal(contract["minimum_from_gamma"]),
            "exchange_order_eligibility_verified": False}


def initial_packet(expected: dict, clock: dict, receipts: list[dict], shares: Decimal, hold_ms: int) -> dict:
    if len(receipts) != 5 or not 0 < shares <= 100 or type(hold_ms) is not int or not 250 <= hold_ms <= 5000:
        raise ValueError("Bounded initial episode inputs required")
    gamma = receipt_body(receipts[0], GAMMA + "/markets/" + expected["market_id"])
    event = receipt_body(receipts[1], GAMMA + "/events/" + expected["event_id"])
    condition = gamma.get("conditionId", "")
    clob = receipt_body(receipts[2], CLOB + "/clob-markets/" + condition)
    contract = terms(expected, gamma, clob, event)
    quotes = {}
    for name, asset, receipt in zip(("UP", "DOWN"), contract["assets"], receipts[3:]):
        try:
            body = receipt_body(receipt, CLOB + "/book?token_id=" + asset)
            quote = book_quote(body, receipt, clock, contract, asset, shares)
            quotes[name] = {"status": "quoted", "quote": quote}
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            quotes[name] = {"status": "unavailable", "reason": str(exc)}
    return {"version": VERSION, "expected": expected, "clock": clock, "shares": str(shares),
            "requested_hold_ms": hold_ms, "terms": contract, "initial_receipts": receipts,
            "initial_quotes": quotes, "purpose": "public_snapshot_execution_diagnostic_only"}


def freeze(packet: dict, local_ms: int, monotonic_ns: int) -> dict:
    # The caller fsyncs this envelope before sleeping or issuing later requests.
    integer(local_ms, "seal time")
    integer(monotonic_ns, "seal monotonic time")
    for receipt in packet["initial_receipts"]:
        if receipt["received_local_ms"] > local_ms or receipt["received_monotonic_ns"] > monotonic_ns:
            raise ValueError("Initial input received after seal")
    utc, uncertainty = clock_utc(packet["clock"], local_ms)
    if utc - uncertainty < packet["expected"]["start_at_ms"] or utc + uncertainty >= packet["expected"]["end_at_ms"]:
        raise ValueError("Seal outside confirmed active market window")
    raw = encoded(packet)
    return {"schema_version": 1, "sealed_local_ms": local_ms, "sealed_monotonic_ns": monotonic_ns,
            "packet_json": raw, "packet_sha256": sha(raw)}


def replay(frozen: dict, later: list[dict]) -> dict:
    raw = frozen.get("packet_json")
    if (type(frozen.get("schema_version")) is not int or frozen["schema_version"] != 1 or
            not isinstance(raw, str) or sha(raw) != frozen.get("packet_sha256")):
        raise ValueError("Sealed intent integrity mismatch")
    packet = json.loads(raw); expected = packet["expected"]; clock = packet["clock"]
    if packet.get("version") != VERSION:
        raise ValueError("Unknown episode version")
    rebuilt = initial_packet(expected, clock, packet["initial_receipts"], number(packet["shares"], "shares"),
                             packet["requested_hold_ms"])
    if rebuilt != packet or freeze(packet, frozen["sealed_local_ms"], frozen["sealed_monotonic_ns"]) != frozen:
        raise ValueError("Sealed intent does not match original inputs")
    if len(later) != 5:
        raise ValueError("Exactly five later public receipts required")
    delay = max(packet["requested_hold_ms"], packet["terms"]["snapshot_hold_ms"])
    for receipt in later:
        if (integer(receipt.get("sent_monotonic_ns"), "later request sent") - frozen["sealed_monotonic_ns"] < delay * 1000000 or
                receipt["sent_local_ms"] < frozen["sealed_local_ms"]):
            raise ValueError("Later request preceded required sealed delay")
    rejection = None
    try:
        gamma = receipt_body(later[0], GAMMA + "/markets/" + expected["market_id"])
        event = receipt_body(later[1], GAMMA + "/events/" + expected["event_id"])
        clob = receipt_body(later[2], CLOB + "/clob-markets/" + packet["terms"]["condition_id"])
        current_terms = terms(expected, gamma, clob, event)
        if current_terms != packet["terms"]:
            raise ValueError("Market terms changed after seal")
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        rejection = str(exc)
    results = {}
    for name, asset, receipt in zip(("UP", "DOWN"), packet["terms"]["assets"], later[3:]):
        original = packet["initial_quotes"][name]
        try:
            if original["status"] != "quoted":
                raise ValueError("Initial quote unavailable: " + original["reason"])
            if rejection:
                raise ValueError(rejection)
            utc, uncertainty = clock_utc(clock, receipt["received_local_ms"])
            if utc + uncertainty >= expected["end_at_ms"]:
                raise ValueError("Market cutoff passed during delay/transport")
            body = receipt_body(receipt, CLOB + "/book?token_id=" + asset)
            quote = book_quote(body, receipt, clock, packet["terms"], asset, Decimal(packet["shares"]),
                               limit=Decimal(original["quote"]["limit_price"]))
            if Decimal(quote["estimated_total_cost"]) > Decimal(original["quote"]["estimated_total_cost"]):
                raise ValueError("Estimated cost exceeds frozen reserve")
            results[name] = {"status": "snapshot_fillable", "quote": quote,
                "frozen_limit_price": original["quote"]["limit_price"],
                "frozen_estimated_reserve": original["quote"]["estimated_total_cost"],
                "actual_request_delay_ms": (receipt["sent_monotonic_ns"] - frozen["sealed_monotonic_ns"]) / 1e6,
                "actual_receive_delay_ms": (receipt["received_monotonic_ns"] - frozen["sealed_monotonic_ns"]) / 1e6,
                "price_slippage_per_share": str(Decimal(quote["average_price"]) - Decimal(original["quote"]["average_price"]))}
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            results[name] = {"status": "rejected", "reason": str(exc)}
    return {"schema_version": 1, "version": VERSION, "market_id": expected["market_id"],
            "sealed_packet_sha256": frozen["packet_sha256"], "minimum_requested_delay_ms": delay,
            "results": results, "limitations": LIMITATIONS, "live_ready": False,
            "exchange_fills_verified": False, "prediction_credit": False, "profitability_credit": False,
            "model_calls": 0, "external_orders_submitted": 0, "production_database_written": False}


def write_new(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush(); os.fsync(stream.fileno())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class PublicReader:
    def __init__(self, directory: Path):
        self.directory = directory; self.receipts = []
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, url: str, *, timeout: float = 5) -> dict:
        if not allowed_url(url) or len(self.receipts) >= MAX_REQUESTS or not 0 < timeout <= 5:
            raise ValueError("Public GET allowlist/budget violation")
        receipt = {"schema_version": 1, "request_number": len(self.receipts) + 1,
                   "method": "GET", "authenticated": False, "url": url,
                   "sent_local_ms": int(time.time_ns() / 1e6), "sent_monotonic_ns": time.monotonic_ns()}
        try:
            request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json",
                "User-Agent": "PolycastPublicExecutionResearch/1.0"})
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_BYTES + 1); receipt["http_status"] = response.status
            if len(raw) > MAX_BYTES:
                raise ValueError("Public response too large")
            receipt["raw_text"] = raw.decode("utf-8"); receipt["raw_sha256"] = sha(raw)
        except (urllib.error.URLError, OSError, ValueError, UnicodeError) as exc:
            receipt["http_status"] = getattr(exc, "code", None)
            receipt["error_type"] = type(exc).__name__
        finally:
            receipt["received_monotonic_ns"] = time.monotonic_ns()
            receipt["received_local_ms"] = int(time.time_ns() / 1e6)
            self.receipts.append(receipt)
            write_new(self.directory / f"receipt-{len(self.receipts):02}.json", receipt)
        return receipt

    def clock_fetch(self, url: str, *, timeout: float = 5) -> bytes:
        receipt = self.get(url, timeout=timeout)
        if receipt.get("http_status") != 200 or "raw_text" not in receipt:
            raise ValueError("Public clock request failed")
        return receipt["raw_text"].encode("utf-8")


def capture(selection_path: Path, output: Path, shares: Decimal, hold_ms: int) -> dict:
    # Exclusive directory claim happens before any network access.
    output.mkdir(parents=True, exist_ok=False)
    reader = PublicReader(output)
    status = "incomplete"; summary = None
    try:
        selection_raw = selection_path.read_bytes()
        selection = json.loads(selection_raw)
        clock = observe_public_clock(fetch=reader.clock_fetch)
        utc, uncertainty = clock_utc(clock, int(time.time_ns() / 1e6))
        candidates = [m for m in selection["markets"] if
            m["start_at_ms"] <= utc - uncertainty and utc + uncertainty + 15000 < m["end_at_ms"]]
        if len(candidates) != 1:
            raise ValueError("Exactly one current market with capture time remaining required")
        expected = candidates[0]
        gamma_receipt = reader.get(GAMMA + "/markets/" + expected["market_id"])
        gamma = receipt_body(gamma_receipt, gamma_receipt["url"])
        event_receipt = reader.get(GAMMA + "/events/" + expected["event_id"])
        condition = gamma.get("conditionId", "")
        clob_receipt = reader.get(CLOB + "/clob-markets/" + condition)
        # Validate the selected version before requesting any token book.
        terms(expected, gamma, receipt_body(clob_receipt, clob_receipt["url"]),
              receipt_body(event_receipt, event_receipt["url"]))
        initial = [gamma_receipt, event_receipt, clob_receipt] + [reader.get(CLOB + "/book?token_id=" + asset)
            for asset in (expected["up_asset"], expected["down_asset"])]
        packet = initial_packet(expected, clock, initial, shares, hold_ms)
        frozen = freeze(packet, int(time.time_ns() / 1e6), time.monotonic_ns())
        write_new(output / "frozen-intent.json", frozen)
        delay = max(hold_ms, packet["terms"]["snapshot_hold_ms"])
        while time.monotonic_ns() - frozen["sealed_monotonic_ns"] < delay * 1000000:
            time.sleep(max(0, delay / 1000 - (time.monotonic_ns() - frozen["sealed_monotonic_ns"]) / 1e9))
        later = [reader.get(GAMMA + "/markets/" + expected["market_id"]),
                 reader.get(GAMMA + "/events/" + expected["event_id"]),
                 reader.get(CLOB + "/clob-markets/" + condition)] + [reader.get(CLOB + "/book?token_id=" + asset)
                 for asset in (expected["up_asset"], expected["down_asset"])]
        summary = replay(frozen, later)
        write_new(output / "replay.json", summary)
        status = "capture_completed"
        return summary
    except (ValueError, KeyError, TypeError, OSError, InvalidOperation) as exc:
        status = "capture_unavailable"
        summary = {"status": status, "reason": str(exc)[:250], "external_orders_submitted": 0,
                   "live_ready": False, "model_calls": 0, "production_database_written": False}
        return summary
    finally:
        manifest = {"schema_version": 1, "version": VERSION, "status": status,
            "selection_sha256": sha(selection_raw) if "selection_raw" in locals() else None,
            "requests": len(reader.receipts), "maximum_requests": MAX_REQUESTS,
            "summary": summary, "receipt_files": [{"filename": f"receipt-{i:02}.json", "sha256": sha(
                (output / f"receipt-{i:02}.json").read_bytes())} for i in range(1, len(reader.receipts) + 1)],
            "later_request_numbers": [x["request_number"] for x in later] if "later" in locals() else [],
            "model_calls": 0, "production_database_written": False, "external_orders_submitted": 0}
        write_new(output / "capture.json", manifest)


def replay_directory(directory: Path) -> dict:
    manifest = json.loads((directory / "capture.json").read_text(encoding="utf-8"))
    if manifest.get("version") != VERSION or manifest.get("status") != "capture_completed":
        raise ValueError("Complete matching capture required")
    entries = manifest["receipt_files"]
    if manifest["requests"] != len(entries) or not 1 <= len(entries) <= MAX_REQUESTS:
        raise ValueError("Capture receipt count mismatch")
    receipts = []
    for i, entry in enumerate(entries, 1):
        if entry["filename"] != f"receipt-{i:02}.json":
            raise ValueError("Receipt sequence/path mismatch")
        raw = (directory / entry["filename"]).read_bytes()
        if sha(raw) != entry["sha256"]:
            raise ValueError("Receipt artifact hash mismatch")
        receipt = json.loads(raw)
        if receipt.get("request_number") != i:
            raise ValueError("Receipt request identity mismatch")
        if receipts and (receipt["sent_monotonic_ns"] < receipts[-1]["received_monotonic_ns"] or
                         receipt["sent_local_ms"] < receipts[-1]["received_local_ms"] - 1):
            raise ValueError("HTTP request ordering/clock mismatch")
        receipts.append(receipt)
    frozen = json.loads((directory / "frozen-intent.json").read_text(encoding="utf-8"))
    packet = json.loads(frozen["packet_json"])
    if packet["initial_receipts"] != receipts[2:7] or manifest["later_request_numbers"] != [8, 9, 10, 11, 12]:
        raise ValueError("Frozen request provenance mismatch")
    verify_clock_receipts(packet["clock"], receipts[:2])
    result = replay(frozen, receipts[7:12])
    if result != manifest["summary"] or result != json.loads((directory / "replay.json").read_text(encoding="utf-8")):
        raise ValueError("Stored replay differs from original captured evidence")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("capture"); start.add_argument("--selection", type=Path, required=True)
    start.add_argument("--output", type=Path, required=True); start.add_argument("--shares", default="5")
    start.add_argument("--hold-ms", type=int, default=250)
    check = sub.add_parser("replay"); check.add_argument("--directory", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        shares = number(args.shares, "shares")
        if not 0 < shares <= 100 or not 250 <= args.hold_ms <= 5000:
            parser.error("shares must be (0,100], hold must be [250,5000] ms")
        result = capture(args.selection, args.output, shares, args.hold_ms)
    else:
        if args.output.exists():
            parser.error("New replay output required")
        result = replay_directory(args.directory); write_new(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k in ("status", "reason", "market_id", "results", "live_ready")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
