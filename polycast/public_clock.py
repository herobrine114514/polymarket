"""Read-only public venue clock intervals, including network and timestamp precision."""
from __future__ import annotations
import hashlib
import json
import math
import time
from .net import fetch_bytes

URLS = {"polymarket": "https://clob.polymarket.com/time", "coinbase": "https://api.exchange.coinbase.com/time"}


def clock_intersection(samples: list[dict]) -> dict:
    if {sample.get("venue") for sample in samples} != set(URLS):
        raise ValueError("Two independent venue observations required")
    lower, upper = -math.inf, math.inf
    for sample in samples:
        sent, received = sample["sent_local_ms"], sample["received_local_ms"]
        source, precision = sample["server_ms"], sample["precision_ms"]
        if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in (sent, received, source, precision)) or
                not 0 <= received - sent <= 2000 or not 0 <= precision <= 1000 or
                isinstance(sample.get("monotonic_elapsed_ms"), bool) or
                not isinstance(sample.get("monotonic_elapsed_ms"), (int, float)) or
                not math.isfinite(sample["monotonic_elapsed_ms"]) or
                abs(sample["monotonic_elapsed_ms"] - (received - sent)) > 100):
            raise ValueError("Invalid clock sample or local wall step")
        lower = max(lower, source - received)
        upper = min(upper, source + precision - sent)
    if lower > upper:
        raise ValueError("Public venue clock intervals disagree")
    offset, uncertainty = (lower + upper) / 2, (upper - lower) / 2
    if abs(offset) > 10000 or uncertainty > 750:
        raise ValueError("Clock offset or uncertainty outside collection limits")
    return {"status": "venues_agree", "offset_ms": offset, "uncertainty_ms": uncertainty,
            "offset_lower_ms": lower, "offset_upper_ms": upper}


def observe_public_clock(fetch=fetch_bytes) -> dict:
    samples = []
    for venue, url in URLS.items():
        sent, monotonic = time.time_ns() / 1e6, time.monotonic_ns()
        value = fetch(url, timeout=5)
        received, elapsed = time.time_ns() / 1e6, (time.monotonic_ns() - monotonic) / 1e6
        if venue == "polymarket":
            number = json.loads(value)
            if type(number) is not int or number <= 0:
                raise ValueError("Invalid public CLOB time")
            source, precision = number * 1000, 1000
        else:
            number = json.loads(value).get("epoch")
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
                raise ValueError("Invalid public Coinbase time")
            source, precision = number * 1000, 1
        samples.append({"venue": venue, "source_url": url, "sent_local_ms": sent, "received_local_ms": received,
                        "server_ms": source, "precision_ms": precision, "monotonic_elapsed_ms": elapsed})
    result = {"schema_version": 1, **clock_intersection(samples), "samples": samples,
              "checked_at_local_ms": int(time.time_ns() / 1e6), "system_clock_changed": False,
              "signed_time_verified": False}
    result["clock_id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result
