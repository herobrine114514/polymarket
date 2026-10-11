"""Shared paper book walking and auditable fee estimates."""
from __future__ import annotations

import math

FEE_METHOD = "per_level_5dp_v1"


def match_levels(book: dict, shares: float, side: str) -> list[dict] | None:
    if not math.isfinite(shares) or shares <= 0 or side not in ("asks", "bids"):
        raise ValueError("Invalid paper fill size or book side")
    levels = sorted(((float(x["price"]), float(x["size"])) for x in book.get(side, [])),
                    reverse=side == "bids")
    remaining = shares
    fills = []
    for price, available in levels:
        if not all(math.isfinite(x) for x in (price, available)) or not 0 < price < 1 or available <= 0:
            continue
        take = min(available, remaining)
        fills.append({"price": price, "shares": take})
        remaining -= take
        if remaining <= 1e-9:
            return fills
    return None


def fill_totals(fills: list[dict], shares: float, rate: float) -> tuple[float, float, float]:
    """Return gross cost, average price, and summed per-level rounded USDC fee.

    A public aggregated book cannot reveal the number of actual maker matches
    within one level. The per-level rounding is an estimate of match-time fees.
    """
    if (not isinstance(fills, list) or not fills or not math.isfinite(shares) or
            shares <= 0 or not math.isfinite(rate) or rate < 0):
        raise ValueError("Invalid paper fill audit")
    gross = count = fees = 0.0
    for item in fills:
        price, size = float(item["price"]), float(item["shares"])
        if (not math.isfinite(price) or not math.isfinite(size) or
                not 0 < price < 1 or size <= 0):
            raise ValueError("Invalid paper fill level")
        gross += price * size
        count += size
        fees += round(size * rate * price * (1 - price), 5)
    if not math.isclose(count, shares, rel_tol=0, abs_tol=1e-8):
        raise ValueError("Paper fill shares mismatch")
    return gross, gross / shares, round(fees, 5)
