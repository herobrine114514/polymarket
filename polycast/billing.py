"""Response usage accounting. Price ranges are estimates, never invoices."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal

PRICING_VERSION = "deepseek-usd-20261002"
PRICING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
# USD per million tokens, checked 2026-10-02 Hong Kong time. Use the whole
# published off-peak/peak range: holiday, crossing-time and invoice rules have
# not been reconciled. Frozen per response, never reprice historical entries.
RATES = {
    "deepseek-v4-pro": {"cache_hit": "0.022", "cache_miss": "0.66", "output": "1.98"},
    "deepseek-flash": {"cache_hit": "0.003", "cache_miss": "0.15", "output": "0.6"},
}
ALIASES = {
    "deepseek-v4-pro": "deepseek-v4-pro", "DeepSeek-V4-Pro-0813": "deepseek-v4-pro",
    "deepseek-flash": "deepseek-flash", "DeepSeek-V4.1-Flash": "deepseek-flash",
}
_usage_sink = ContextVar("polycast_usage_sink", default=None)


@contextmanager
def capture_usage(sink):
    """Bind a response sink to this call/thread, not to the model JSON output."""
    token = _usage_sink.set(sink)
    try:
        yield
    finally:
        _usage_sink.reset(token)


def _count(value):
    return value if type(value) is int and 0 <= value <= 100_000_000 else None


def deepseek_usage(response: object, requested_model: str) -> dict:
    received_at = datetime.now(timezone.utc).isoformat()
    raw = response if isinstance(response, dict) else {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    names = ("prompt_tokens", "completion_tokens", "total_tokens",
             "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    counts = {name: _count(usage.get(name)) for name in names}
    # Reasoning tokens are already included in completion_tokens; never add twice.
    details = usage.get("completion_tokens_details")
    reasoning = _count(details.get("reasoning_tokens")) if isinstance(details, dict) else None
    returned_model = raw.get("model")
    returned_model = returned_model if isinstance(returned_model, str) and returned_model in ALIASES else None
    result = {"provider": "deepseek", "received_at": received_at,
              "returned_model": returned_model, **counts,
              "reasoning_tokens": reasoning, "usage_status": "incomplete",
              "cost_lower_usd": None, "cost_upper_usd": None, "pricing_snapshot": None}
    if all(value is not None for value in counts.values()):
        valid = (counts["prompt_tokens"] == counts["prompt_cache_hit_tokens"] + counts["prompt_cache_miss_tokens"] and
                 counts["total_tokens"] == counts["prompt_tokens"] + counts["completion_tokens"] and
                 (reasoning is None or reasoning <= counts["completion_tokens"]))
        result["usage_status"] = "measured" if valid else "invalid"
        model = ALIASES.get(returned_model)
        if valid and model == ALIASES.get(requested_model) and model in RATES:
            rates = RATES[model]
            lower = (Decimal(counts["prompt_cache_hit_tokens"]) * Decimal(rates["cache_hit"]) +
                     Decimal(counts["prompt_cache_miss_tokens"]) * Decimal(rates["cache_miss"]) +
                     Decimal(counts["completion_tokens"]) * Decimal(rates["output"])) / Decimal(1_000_000)
            result.update(cost_lower_usd=str(lower), cost_upper_usd=str(lower * 2),
                          pricing_snapshot={"version": PRICING_VERSION, "source": PRICING_SOURCE,
                                            "currency": "USD", "model": model,
                                            "off_peak_per_million": rates,
                                            "peak_multiplier": 2, "method": "published_rate_range",
                                            "invoice_reconciled": False})
    elif any(name in usage and counts[name] is None for name in names):
        result["usage_status"] = "invalid"
    return result


def emit_deepseek_usage(response: object, requested_model: str) -> None:
    sink = _usage_sink.get()
    if sink is not None:
        sink(deepseek_usage(response, requested_model))


def save_model_usage(conn: sqlite3.Connection, call_id: int, item: dict) -> None:
    call = conn.execute("SELECT model_version FROM model_runs WHERE id=?", (call_id,)).fetchone()
    if not call or not call["model_version"].startswith("deepseek") or item["provider"] != "deepseek":
        raise ValueError("Usage does not belong to this DeepSeek call")
    # One immutable response receipt per reserved remote call. Identical receipts
    # may be retried; a different second response cannot silently overwrite it.
    encoded = json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
    prior = conn.execute("SELECT receipt_json FROM model_usage WHERE call_id=?", (call_id,)).fetchone()
    if prior:
        if prior["receipt_json"] != encoded:
            raise ValueError("A different usage receipt already exists")
        return
    conn.execute("""INSERT INTO model_usage(call_id,received_at,usage_status,
        prompt_tokens,completion_tokens,total_tokens,prompt_cache_hit_tokens,
        prompt_cache_miss_tokens,reasoning_tokens,returned_model,cost_lower_usd,
        cost_upper_usd,receipt_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (call_id, item["received_at"], item["usage_status"], item["prompt_tokens"],
         item["completion_tokens"], item["total_tokens"], item["prompt_cache_hit_tokens"],
         item["prompt_cache_miss_tokens"], item["reasoning_tokens"], item["returned_model"],
         item["cost_lower_usd"], item["cost_upper_usd"], encoded))


def cost_summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("""SELECT r.id,r.model_version,r.status,u.received_at,
        u.usage_status,u.prompt_tokens,u.completion_tokens,u.cost_lower_usd,u.cost_upper_usd
        FROM model_runs r LEFT JOIN model_usage u ON u.call_id=r.id ORDER BY r.id""").fetchall()
    priced = [r for r in rows if r["cost_lower_usd"] is not None and r["cost_upper_usd"] is not None]
    measured = [r for r in rows if r["usage_status"] == "measured"]
    lower = sum((Decimal(r["cost_lower_usd"]) for r in priced), Decimal(0))
    upper = sum((Decimal(r["cost_upper_usd"]) for r in priced), Decimal(0))
    return {"scope": "All reserved calls, including failed analysis/review; only received usage is priced. Historical/Codex/timeout costs may be unknown.",
            "currency": "USD", "pricing_version": PRICING_VERSION,
            "total_calls": len(rows), "received_usage_calls": sum(r["usage_status"] is not None for r in rows),
            "measured_usage_calls": len(measured), "priced_calls": len(priced),
            "unpriced_calls": len(rows) - len(priced),
            "invalid_usage_calls": sum(r["usage_status"] == "invalid" for r in rows),
            "recorded_prompt_tokens": sum(r["prompt_tokens"] for r in measured),
            "recorded_completion_tokens": sum(r["completion_tokens"] for r in measured),
            "recorded_cost_lower_usd": float(lower) if priced else None,
            "recorded_cost_upper_usd": float(upper) if priced else None,
            "first_received_at": min((r["received_at"] for r in rows if r["received_at"]), default=None),
            "last_received_at": max((r["received_at"] for r in rows if r["received_at"]), default=None),
            "model_cost_coverage_complete": bool(rows) and len(priced) == len(rows),
            "invoice_reconciled": False, "all_in_net_pnl": None,
            "all_in_net_pnl_reason": "Model billing and historical costs are incomplete; data/infrastructure costs have not been reconciled."}
