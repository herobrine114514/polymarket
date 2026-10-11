"""Public Exchange component states as data-quality context, never price truth."""
from __future__ import annotations

from datetime import datetime, timezone

from .market import parse_utc

COINBASE_STATUS = "https://status.exchange.coinbase.com/api/v2/summary.json"
PAGE_ID = "bklmvp2c52bl"
COMPONENTS = {"1x5x3dcdkp4p": "Coinbase Exchange REST API",
              "j1b4fl9w7fcw": "Markets", "xffjmgtm0dgw": "BTC-USD"}
STATUSES = {"operational", "degraded_performance", "partial_outage", "major_outage",
            "under_maintenance"}


def coinbase_status_evidence(payload: dict, market: dict, observed_at: datetime) -> dict:
    """Use the actual local observation time; changed-at is not a news release time."""
    if (observed_at.tzinfo is None or market.get("category") != "crypto5m"
            or not str(market.get("source_ref", "")).startswith("BTCUSD:")):
        raise ValueError("Exchange context requires a BTC five-minute market and aware observation time")
    page = payload["page"]
    if page["id"] != PAGE_ID or page["name"] != "Coinbase Exchange":
        raise ValueError("Wrong Coinbase Exchange status page")
    changed = parse_utc(page.get("updated_at"))
    if changed is None or changed > observed_at:
        raise ValueError("Missing or future status-page change time")
    rows = payload["components"]
    if not isinstance(rows, list):
        raise ValueError("Invalid Exchange component list")
    selected = {}
    for row in rows:
        ident = row.get("id") if isinstance(row, dict) else None
        if ident not in COMPONENTS:
            continue
        if (ident in selected or row.get("name") != COMPONENTS[ident]
                or row.get("status") not in STATUSES):
            raise ValueError("Unknown, duplicated or mismatched Exchange component")
        updated = parse_utc(row.get("updated_at"))
        if updated is None or updated > observed_at:
            raise ValueError("Missing or future Exchange component change time")
        selected[ident] = row
    if set(selected) != set(COMPONENTS):
        raise ValueError("Required Exchange component missing; status is unknown")
    timestamp = observed_at.astimezone(timezone.utc).isoformat(timespec="microseconds")
    states = "; ".join(f"{name}: {selected[ident]['status']} "
                       f"(last reported change {selected[ident]['updated_at']})"
                       for ident, name in COMPONENTS.items())
    return {"source": "Coinbase Exchange service status", "source_id": "coinbase_exchange_status",
            "feed_url": COINBASE_STATUS, "license_scope": "official_source",
            "url": COINBASE_STATUS + f"#market-{market['id']}",
            "origin_story_id": market["source_ref"],
            "title": f"Coinbase Exchange Bitcoin BTC-USD data-source status for market {market['id']}",
            "summary": (f"Official public service-state observation at {timestamp}. {states}. "
                        f"Page last reported change: {page['updated_at']}. "
                        "Local retrieval time is not the incident start or a new announcement. "
                        "Reported operational status does not prove a quote is correct or fresh; "
                        "reported degradation is a data-quality risk, not a directional price signal. "
                        "This does not independently verify Chainlink signed prices or settlement."),
            "published_at": timestamp, "updated_at": timestamp}
