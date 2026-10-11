"""Offline SDK 0.12.0 receipt bridge into the synthetic execution journal.

Only the explicit single-intent, dedicated-account fixture scope is accepted.
No transport, credentials, signing, production imports, or live fee attestation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from experiments.execution_replay import (ExecutionReplay, ReplayError, ReconcilePending,
                                         amount, integer, pending, require, text_id)

SDK_SCOPE = "synthetic_secure_sdk_transport_and_normalization_only"


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def fixture_bundle(report: dict) -> dict:
    require(report.get("scope") == SDK_SCOPE and report.get("sdk_version") == "0.12.0",
            "Only the pinned synthetic SDK fixture is supported")
    require(report.get("native_network_calls") == 0 and report.get("real_credentials_used") is False
            and report.get("user_wallet_used") is False and report.get("real_orders_submitted") == 0,
            "Real account observations are not admitted by this research bridge")
    return copy.deepcopy({"scope": SDK_SCOPE, "sdk_version": "0.12.0",
                          "sdk_normalized": report["sdk_normalized"],
                          "requests": report["requests"], "account": report["fixture_account"]})


def fixture_reservation(report: dict, intent_id: str = "fixture-intent") -> dict:
    fixture_bundle(report)
    return {**copy.deepcopy(report["frozen_identity"]), "intent_id": text_id(intent_id),
            "risk_group": "synthetic-btc-window", "api_owner": report["sdk_normalized"]["order"]["owner"]}


def paged_items(bundle: dict, field: str, route: str) -> list[dict]:
    pages = bundle["sdk_normalized"][field]
    pending(isinstance(pages, list) and 0 < len(pages) <= 100, "Missing or excessive SDK pages")
    requests = [r for r in bundle["requests"] if r.get("method") == "GET" and r.get("path") == route]
    pending(len(requests) == len(pages), "SDK pages and request receipts do not agree")
    seen_cursors, expected, result = set(), None, []
    for index, (page, request) in enumerate(zip(pages, requests)):
        require(isinstance(page, dict) and isinstance(page.get("items"), list), "Malformed SDK page")
        require(type(page.get("hasMore")) is bool, "SDK hasMore must be boolean")
        require(request.get("query") == ({} if expected is None else {"next_cursor": expected}),
                "Missing, repeated, filtered, or reordered page request")
        require(request.get("hmac_independently_validated") is True, "Fixture auth receipt missing")
        require(page["hasMore"] is (index < len(pages) - 1), "Page chain is not complete and terminal")
        if page["hasMore"]:
            expected = text_id(page.get("nextCursor"))
            require(expected not in seen_cursors and expected != "LTE=", "Invalid or repeated page cursor")
            seen_cursors.add(expected)
        else:
            require(page.get("nextCursor") in {None, "LTE="}, "Nonterminal cursor on final page")
        result.extend(page["items"])
        require(len(result) <= 1000, "Excessive SDK receipt count")
    return result


def asset(row: dict) -> str:
    # 0.12.0 returns assetId plus its deprecated tokenId alias.
    token = text_id(row["assetId"])
    require("tokenId" not in row or row["tokenId"] == token, "Conflicting assetId/tokenId aliases")
    return token


def normalized_payload(bundle: dict, intent: dict) -> dict:
    require(bundle.get("scope") == SDK_SCOPE and bundle.get("sdk_version") == "0.12.0",
            "Research fixture scope/version mismatch")
    require(intent["order_type"] == "FOK", "This bridge supports synthetic BUY taker FOK only")
    order = bundle["sdk_normalized"]["order"]
    require(order["makerAddress"].lower() == intent["funder"].lower()
            and order["owner"] == intent["api_owner"], "Order belongs to a different fixture account")
    mapped_order = {k: order[k] for k in ("id", "conditionId", "side", "originalSize", "price",
                                        "orderType", "status", "associateTrades", "sizeMatched")}
    mapped_order["assetId"] = asset(order)
    require(all(isinstance(value, str) for key, value in mapped_order.items()
                if key != "associateTrades"), "Malformed SDK order field types")
    associated = mapped_order["associateTrades"]
    require(isinstance(associated, list) and len(set(associated)) == len(associated),
            "Duplicate or malformed associated trade identities")
    open_orders = paged_items(bundle, "open_order_pages", "/data/orders")
    trades = paged_items(bundle, "account_trade_pages", "/data/trades")
    account = bundle["account"]
    pending(account.get("scope") == "synthetic_dedicated_account_observation",
            "Dedicated synthetic account receipt required")
    receipts = account["fee_receipts"]
    require(isinstance(receipts, dict), "Malformed fee receipt set")
    require(isinstance(account["positions"], dict), "Malformed account position set")
    for token, size in account["positions"].items():
        text_id(token)
        amount(size)
    mapped, seen = [], set()
    for row in trades:
        ident = text_id(row["id"])
        require(ident not in seen, "Duplicate SDK trade receipt")
        seen.add(ident)
        require(row["makerAddress"].lower() == intent["funder"].lower()
                and row["owner"] == intent["api_owner"], "Trade belongs to a different fixture account")
        require(row["takerOrderId"] == intent["expected_order_id"], "Foreign trade on single-intent fixture account")
        token = asset(row)
        pending(ident in receipts, "Measured fee receipt missing; feeRateBps is not a paid fee")
        fee = receipts[ident]
        require(fee.get("scope") == "synthetic_measured_fee_receipt", "Unverified fee receipt scope")
        require(fee.get("trade_id") == ident and fee.get("order_id") == row["takerOrderId"]
                and fee.get("condition_id") == row["conditionId"] and fee.get("token_id") == token
                and fee.get("funder", "").lower() == intent["funder"].lower()
                and fee.get("transaction_hash") == row["transactionHash"]
                and fee.get("status") == row["status"].removeprefix("TRADE_STATUS_"),
                "Fee receipt does not bind the trade/account/transaction/finality")
        paid = integer(fee["fee_paid_micro"])
        require(row["status"] != "TRADE_STATUS_FAILED" or paid == 0, "Failed trade has an unexplained debit")
        mapped.append({"id": ident, "takerOrderId": row["takerOrderId"], "market": row["conditionId"],
                       "tokenId": token, "side": row["side"], "traderSide": row["traderSide"],
                       "price": row["price"], "size": row["size"], "status": row["status"],
                       "fee_paid_micro": paid})
    require(set(receipts) == seen, "Fee receipt set does not match the observed trade set")
    require(sum(t["fee_paid_micro"] for t in mapped) <= intent["maximum_fee_micro"], "Frozen fee cap exceeded")
    return {"complete": True, "order": mapped_order, "trades": mapped,
            "open_order_ids": [text_id(row["id"]) for row in open_orders],
            "cash_micro": integer(account["cash_micro"]), "positions": account["positions"]}


def reconcile_sdk(replay: ExecutionReplay, event_id: str, intent_id: str, bundle: dict, *,
                  observed_seq: int, captured_at: int, checked_at: int) -> dict:
    """Journal a failed observation as pending, keeping the entire external hold.

    Economic/identity mismatches in otherwise valid receipts are independently
    rejected by the journal and persist FAULT. Neither path can resubmit orders.
    """
    require(type(observed_seq) is int and observed_seq >= 0, "Invalid observation journal sequence")
    state = replay.snapshot()
    require(intent_id in state["intents"], "Unknown intent")
    observation_hash = digest(bundle)
    try:
        require(bundle["account"]["capital_micro"] == state["capital_micro"], "Different account baseline")
        payload = normalized_payload(bundle, state["intents"][intent_id])
    except (ReplayError, KeyError, TypeError, AttributeError) as exc:
        reason = str(exc) if isinstance(exc, ReplayError) else "Malformed SDK/account receipt"
        payload = {"complete": False, "incomplete_reason": reason}
    return replay.command(event_id, "reconcile", {**payload, "intent_id": intent_id,
                          "observed_seq": observed_seq, "captured_at": captured_at,
                          "checked_at": checked_at, "observation_sha256": observation_hash})


def acknowledge_sdk(replay: ExecutionReplay, event_id: str, intent_id: str, ack: dict) -> dict:
    # ACK is not finality or account reconciliation, even when status is matched.
    return replay.command(event_id, "ack", {"intent_id": intent_id,
                          "order_id": ack.get("orderId", "invalid"),
                          "status": ack.get("status", "invalid") if ack.get("ok") is True else "invalid"})


def run_fixture(report_path: Path, output: Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    output = output.resolve()
    require(output.is_relative_to(root / ".polycast") and not output.exists(), "Use a new isolated artifact directory")
    raw = report_path.read_bytes()
    require(len(raw) <= 1_000_000, "Fixture report too large")
    report = json.loads(raw)
    bundle, reservation = fixture_bundle(report), fixture_reservation(report)
    output.mkdir(parents=True)
    (output / "registered-bridge.py").write_bytes(Path(__file__).read_bytes())
    (output / "registered-journal.py").write_bytes((root / "experiments/execution_replay.py").read_bytes())
    (output / "sdk-fixture.json").write_bytes(raw)
    replay = ExecutionReplay.create(output / "journal.sqlite3")
    replay.command("reserve", "reserve", reservation)
    replay.command("begin", "begin_submit", {"intent_id": "fixture-intent"})
    acknowledge_sdk(replay, "ack", "fixture-intent", report["ack"])
    before = replay.snapshot()
    no_fees = copy.deepcopy(bundle)
    no_fees["account"]["fee_receipts"] = {}
    now = int(datetime.now(timezone.utc).timestamp())
    try:
        reconcile_sdk(replay, "missing-fee", "fixture-intent", no_fees,
                      observed_seq=before["seq"], captured_at=now, checked_at=now)
        raise AssertionError("Missing fee observation unexpectedly accepted")
    except ReconcilePending:
        pass
    replay = ExecutionReplay.open(replay.path)
    held = replay.snapshot()
    require(held["risk"]["held_micro"] == reservation["reserve_micro"] and held["risk"]["halted"],
            "Missing fee released funds")
    seq = held["seq"]
    reconciled = reconcile_sdk(replay, "complete", "fixture-intent", bundle,
                              observed_seq=seq, captured_at=now, checked_at=now)
    replay = ExecutionReplay.open(replay.path)
    duplicated = reconcile_sdk(replay, "complete", "fixture-intent", bundle,
                              observed_seq=seq, captured_at=now, checked_at=now)
    require(duplicated["duplicate"] is True and reconciled["risk"]["held_micro"] == 0
            and duplicated["risk"]["position_cost_micro"] == 2_687_350, "Replay/duplicate accounting failed")
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "scope": "synthetic_sdk_journal_bridge_only",
              "sdk_fixture_sha256": hashlib.sha256(raw).hexdigest(), "observation_sha256": digest(bundle),
              "missing_fee_after_restart": {"phase": held["intents"]["fixture-intent"]["phase"], "risk": held["risk"]},
              "complete_after_restart": {"phase": duplicated["intents"]["fixture-intent"]["phase"], "risk": duplicated["risk"]},
              "duplicate_safely_ignored": duplicated["duplicate"], "submission_attempts": duplicated["intents"]["fixture-intent"]["submit_attempts"],
              "native_network_calls": 0, "real_orders_submitted": 0, "production_database_written": False,
              "real_fee_receipt_verified": False, "real_account_gate_passed": False,
              "source_hashes": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in [Path(__file__), root / "experiments/execution_replay.py"]}}
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk-fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_fixture(args.sdk_fixture, args.output), indent=2))
