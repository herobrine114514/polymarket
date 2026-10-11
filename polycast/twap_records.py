"""Exact relay decoding and receipt-time filtering, independent of settlement truth."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path


def _milliseconds(value: object) -> int:
    if type(value) is not int or not 0 < value < 10**16:
        raise ValueError("Invalid millisecond timestamp")
    return value


def decode_twap_record(item: dict, *, require_receipt: bool = True) -> dict:
    """Old untagged records are RTDS E18; new formats require explicit metadata."""
    if not isinstance(item, dict):
        raise ValueError("TWAP record must be an object")
    if 'schema_version' in item and (type(item['schema_version']) is not int or item['schema_version'] != 1):
        raise ValueError('Unsupported observation schema version')
    timestamp = _milliseconds(item.get("timestamp_ms"))
    receipt = item.get("received_at_ms")
    if receipt is None and require_receipt:
        raise ValueError("Missing observation receipt time")
    if receipt is not None:
        receipt = _milliseconds(receipt)
        if receipt < timestamp - 5000:
            raise ValueError("Source clock exceeds receipt clock tolerance")
    protocol = item.get("feed_protocol", "rtds")
    encoding = item.get("price_encoding", "e18")
    allowed = {"rtds": "e18", "polybolt_raw_v1": "decimal_usd",
               "polybolt_sdk_v1": "decimal_usd"}
    if protocol not in allowed or encoding != allowed[protocol]:
        raise ValueError("Unknown or inconsistent price protocol/encoding")
    full = item.get("full_accuracy_value")
    if not isinstance(full, str):
        raise ValueError("Exact price must be a string")
    pattern = r"[0-9]{15,30}" if encoding == "e18" else r"[0-9]{1,10}(?:\.[0-9]{1,30})?"
    if not re.fullmatch(pattern, full):
        raise ValueError("Invalid exact price encoding")
    with localcontext() as context:
        context.prec = 64
        price = Decimal(full) / (Decimal(10**18) if encoding == "e18" else Decimal(1))
    if price <= 0:
        raise ValueError("Price must be positive")
    if (item.get("symbol", "btcusd") != "btcusd" or
            item.get("quote_currency", "USD") != "USD" or
            item.get("window_seconds", 60) != 60 or
            item.get("price_source", "chainlink") != "chainlink"):
        raise ValueError("Wrong source, symbol, quote currency or window")
    if item.get("sequence_contiguous", True) is not True:
        raise ValueError("Unrecovered sequence discontinuity")
    if protocol != "rtds":
        if (not isinstance(item.get("connection_id"), str) or not item["connection_id"] or
                type(item.get("channel_seq")) is not int or item["channel_seq"] < 0):
            raise ValueError("PolyBolt connection/sequence metadata missing")
    return {"timestamp_ms": timestamp, "received_at_ms": receipt, "price": price,
            "feed_protocol": protocol, "price_encoding": encoding}


def read_twap_records(path: Path, start: datetime, source_until: datetime,
                      *, available_at: datetime | None = None) -> tuple[list[dict], dict]:
    """Use only information actually received by the cutoff; reject ambiguous prices.

    Settlement diagnostics may inspect a past source interval with today's receipt
    cutoff. Forecasting defaults to identical source and availability cutoffs.
    """
    cutoff = available_at or source_until
    if any(time.tzinfo is None or time.utcoffset() is None for time in (start, source_until, cutoff)):
        raise ValueError("TWAP cutoffs must be timezone-aware")
    lower = int(start.timestamp() * 1000) - 3000
    upper = int(source_until.timestamp() * 1000)
    available_ms = int(cutoff.timestamp() * 1000)
    counts: Counter = Counter()
    points: dict[int, dict] = {}
    conflicts: set[int] = set()
    if not path.is_file():
        return [], {"missing_file": 1}
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    # Filter source interval before decoding unrelated historical rows.
                    if not isinstance(item, dict):
                        raise ValueError("Not an object")
                    stamp = _milliseconds(item.get("timestamp_ms"))
                    if not lower <= stamp <= upper:
                        continue
                    point = decode_twap_record(item)
                    if point["received_at_ms"] > available_ms:
                        counts["not_received_by_cutoff"] += 1
                        continue
                    if stamp > available_ms:
                        counts["source_after_cutoff"] += 1
                        continue
                    previous = points.get(stamp)
                    if previous is not None:
                        counts["duplicate_timestamp"] += 1
                        if previous["price"] != point["price"]:
                            conflicts.add(stamp)
                        elif point["received_at_ms"] < previous["received_at_ms"]:
                            points[stamp] = point
                    else:
                        points[stamp] = point
                except (ValueError, TypeError, KeyError):
                    counts["invalid_record"] += 1
    except OSError:
        return [], {"read_error": 1}
    counts["conflicting_timestamps"] = len(conflicts)
    rows = [point for stamp, point in sorted(points.items()) if stamp not in conflicts]
    counts["accepted_points"] = len(rows)
    return rows, dict(counts)
