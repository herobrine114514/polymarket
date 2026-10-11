"""Offline, synthetic BUY/taker execution journal. Not a live trading adapter.

No network, wallet, signing, credentials or production store imports. Each command
is one durable SQLite transaction. Restart reconstructs risk from the journal.
Only a complete, fresh synthetic account reconciliation releases external holds.
Fee-paid fields are synthetic reconciliation receipts, not SDK-provided fields.
Schema 2 verifies request/event digests, a sequence chain and its committed head.
This detects corruption or out-of-band edits, not a malicious complete rewrite.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

TERMINAL_TRADES = {"CONFIRMED", "FAILED"}
TRADE_STATUSES = TERMINAL_TRADES | {"MATCHED", "MATCHED_NOT_BROADCASTED", "MINED", "RETRYING"}
UNCERTAIN = {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "FAULT"}


class ReplayError(ValueError):
    pass


class ReconcilePending(ReplayError):
    """Incomplete/lagging observations; hold funds and permit a later fresh read."""


def pending(condition: bool, message: str) -> None:
    if not condition:
        raise ReconcilePending(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReplayError(message)


def amount(value: str, *, positive: bool = False) -> Decimal:
    require(isinstance(value, str), "Decimal values must be strings")
    try:
        result = Decimal(value)
    except Exception as exc:
        raise ReplayError("Invalid decimal") from exc
    require(result.is_finite() and result >= 0 and (not positive or result > 0), "Invalid decimal")
    require(result.as_tuple().exponent >= -6, "Replay supports at most six decimal places")
    return result


def micro(value: Decimal) -> int:
    return int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def integer(value: int) -> int:
    require(type(value) is int and value >= 0, "Non-negative integer micro amount required")
    return value


def text_id(value: str) -> str:
    require(isinstance(value, str) and 0 < len(value) <= 160 and value.isascii(), "Invalid identity")
    return value


def canon(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def hashed(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def chain_hash(seq: int, event_id: str, request_hash: str, event_hash: str, previous: str) -> str:
    return hashed(canon({"seq": seq, "event_id": event_id, "request_sha256": request_hash,
                         "event_sha256": event_hash, "previous_sha256": previous}))


def normalized_trade(trade: dict, intent: dict) -> dict:
    allowed = {"id", "takerOrderId", "market", "tokenId", "side", "size", "price",
               "status", "traderSide", "fee_paid_micro"}
    require(set(trade) == allowed, "Synthetic trade receipt has missing or unknown fields")
    require(isinstance(trade["status"], str), "Invalid trade status type")
    require(trade["takerOrderId"] == intent["expected_order_id"], "Trade order identity mismatch")
    require(trade["market"] == intent["condition_id"], "Trade condition mismatch")
    require(trade["tokenId"] == intent["token_id"], "Trade token mismatch")
    require(trade["side"] == "BUY" and trade["traderSide"] == "TAKER", "Replay supports BUY taker only")
    price, size = amount(trade["price"], positive=True), amount(trade["size"], positive=True)
    require(price <= amount(intent["limit_price"]) and price < 1, "Fill exceeds frozen limit")
    status = trade["status"].removeprefix("TRADE_STATUS_")
    require(status in TRADE_STATUSES, "Unknown trade status")
    fee = integer(trade["fee_paid_micro"])
    return {"id": text_id(trade["id"]), "size": format(size.normalize(), "f"),
            "price": format(price.normalize(), "f"),
            "status": status, "fee_paid_micro": fee, "cost_micro": micro(price * size) + fee}


def merge_trade(intent: dict, trade: dict) -> None:
    previous = intent["trades"].get(trade["id"])
    if previous:
        for key in ("size", "price", "fee_paid_micro", "cost_micro"):
            require(previous[key] == trade[key], "Trade economic terms changed")
        if previous["status"] in TERMINAL_TRADES:
            require(trade["status"] not in TERMINAL_TRADES or trade["status"] == previous["status"],
                    "Conflicting terminal trade states")
            return  # A late MATCHED/MINED cannot undo CONFIRMED or FAILED.
    intent["trades"][trade["id"]] = trade
    total = sum((amount(t["size"]) for t in intent["trades"].values()), Decimal(0))
    require(total <= amount(intent["shares"]), "Matched shares exceed intended size")
    require(sum(t["cost_micro"] for t in intent["trades"].values()) <= intent["reserve_micro"],
            "Fill and fee receipts exceed reserved budget")


def risk(state: dict) -> dict:
    holds, positions, groups = 0, 0, {}
    for item in state["intents"].values():
        hold = item["reserve_micro"] if item["phase"] not in {"DONE", "LOCAL_EXPIRED"} else 0
        position = item["accounted_cost_micro"]
        holds += hold
        positions += position
        groups[item["risk_group"]] = groups.get(item["risk_group"], 0) + hold + position
    return {"held_micro": holds, "position_cost_micro": positions, "total_risk_micro": holds + positions,
            "group_risk_micro": groups, "halted": state["stream_gap"] or
            any(x["phase"] in UNCERTAIN for x in state["intents"].values())}


def apply(state: dict, event: dict) -> None:
    kind, payload = event["type"], event["payload"]
    if kind == "reserve":
        require(not risk(state)["halted"], "Account has unresolved execution or stream gap")
        require(payload["intent_id"] not in state["intents"], "Intent already exists")
        require(payload["order_type"] in {"FOK", "FAK"}, "Unsupported order type")
        for key in ("intent_id", "expected_order_id", "risk_group", "condition_id", "token_id"):
            text_id(payload[key])
        shares, price = amount(payload["shares"], positive=True), amount(payload["limit_price"], positive=True)
        require(price < 1, "Invalid limit")
        reserve = integer(payload["reserve_micro"])
        require(reserve >= micro(shares * price) + integer(payload["maximum_fee_micro"]),
                "Reserve does not cover limit cost and fees")
        current = risk(state)
        require(current["total_risk_micro"] + reserve <= state["risk_limit_micro"], "Account risk limit exceeded")
        require(current["total_risk_micro"] + reserve <= state["capital_micro"], "Capital exceeded")
        require(current["group_risk_micro"].get(payload["risk_group"], 0) + reserve <= state["group_limit_micro"],
                "Event group limit exceeded")
        require(not any(x["expected_order_id"] == payload["expected_order_id"] for x in state["intents"].values()),
                "Order identity already assigned")
        state["intents"][payload["intent_id"]] = {**payload, "phase": "RESERVED", "submit_attempts": 0,
            "trades": {}, "cancel_requested": False, "accounted_cost_micro": 0, "faults": []}
        return
    if kind == "stream_gap":
        state["stream_gap"] = True
        return
    intent = state["intents"].get(payload["intent_id"])
    require(intent is not None, "Unknown intent")
    if kind == "fault":
        intent["phase"] = "FAULT"
        intent["faults"].append(payload["reason"])
        return
    if kind == "reconcile_deferred":
        if intent["phase"] != "FAULT":
            intent["phase"] = "UNKNOWN"
        return
    if kind == "begin_submit":
        require(intent["phase"] == "RESERVED", "Never automatically resubmit an external intent")
        require(not risk(state)["halted"], "Unresolved account state")
        intent["phase"] = "SUBMITTING"
        intent["submit_attempts"] += 1
    elif kind in {"timeout", "observation_error"}:
        require(intent["phase"] in UNCERTAIN, "No external execution to mark unknown")
        if kind == "observation_error":
            require(payload["reason"] in {"http_404", "http_401", "network_timeout"}, "Unknown observation error")
        if intent["phase"] != "FAULT":
            intent["phase"] = "UNKNOWN"
    elif kind == "ack":
        require(intent["phase"] in {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED"}, "Invalid acknowledgement phase")
        require(payload["order_id"] == intent["expected_order_id"], "Acknowledgement order identity mismatch")
        require(payload["status"] in {"live", "matched", "delayed", "unmatched"}, "Unknown acknowledgement")
        intent["phase"] = "ACKNOWLEDGED"
    elif kind == "cancel_request":
        require(intent["phase"] in UNCERTAIN, "No external order to cancel")
        intent["cancel_requested"] = True
    elif kind == "local_expire":
        require(intent["phase"] == "RESERVED", "Local timeout cannot release an external hold")
        intent["phase"] = "LOCAL_EXPIRED"
    elif kind == "trade":
        require(intent["phase"] not in {"RESERVED", "LOCAL_EXPIRED"}, "Trade before external submission")
        trade = normalized_trade(payload["trade"], intent)
        previous = intent["trades"].get(trade["id"])
        if intent["phase"] == "DONE":
            require(previous is not None and previous["status"] in TERMINAL_TRADES,
                    "New trade after completed reconciliation")
        merge_trade(intent, trade)
    elif kind == "reconcile":
        require(intent["phase"] in UNCERTAIN and intent["phase"] != "FAULT", "Fault requires separate manual recovery")
        pending(payload["complete"] is True, payload.get("incomplete_reason",
                "All REST pages and dedicated account observations required"))
        pending(type(payload["captured_at"]) is int and type(payload["checked_at"]) is int and
                0 <= payload["checked_at"] - payload["captured_at"] <= 30, "Stale or future snapshot")
        order = payload["order"]
        require({"id", "assetId", "conditionId", "side", "originalSize", "price", "orderType",
                 "status", "associateTrades", "sizeMatched"} <= set(order), "REST order fields missing")
        require(order["id"] == intent["expected_order_id"] and order["assetId"] == intent["token_id"] and
                order["conditionId"] == intent["condition_id"] and order["side"] == "BUY", "REST order identity mismatch")
        require(amount(order["originalSize"]) == amount(intent["shares"]) and
                amount(order["price"]) == amount(intent["limit_price"]) and
                order["orderType"] == intent["order_type"], "REST order terms mismatch")
        pending(order["status"] in {"MATCHED", "CANCELED"}, "Order is not authoritatively terminal")
        reported = [normalized_trade(t, intent) for t in payload["trades"]]
        require(len({t["id"] for t in reported}) == len(reported), "Duplicate trade in REST snapshot")
        pending(set(order["associateTrades"]) == {t["id"] for t in reported}, "Missing associated trade page")
        pending(set(intent["trades"]) <= {t["id"] for t in reported}, "REST omitted a locally observed trade")
        for trade in reported:
            merge_trade(intent, trade)
        pending(all(t["status"] in TERMINAL_TRADES for t in reported), "Trade finality still pending")
        matched = sum((amount(t["size"]) for t in reported), Decimal(0))
        require(matched == amount(order["sizeMatched"]), "Order and trade matched sizes differ")
        confirmed = [t for t in reported if t["status"] == "CONFIRMED"]
        filled = sum((amount(t["size"]) for t in confirmed), Decimal(0))
        if intent["order_type"] == "FOK":
            require(filled in {Decimal(0), amount(intent["shares"])}, "Unexpected FOK partial confirmation")
        if order["status"] == "MATCHED":
            require(matched == amount(intent["shares"]), "MATCHED order still has unmatched remainder")
        pending(payload["open_order_ids"] == [], "Unreconciled open orders on dedicated account")
        require(not any(x["phase"] in UNCERTAIN and x is not intent for x in state["intents"].values()),
                "Another external intent is unresolved")
        cost = sum(t["cost_micro"] for t in confirmed)
        expected_cash = state["capital_micro"] - cost - sum(
            x["accounted_cost_micro"] for x in state["intents"].values() if x is not intent)
        require(integer(payload["cash_micro"]) == expected_cash, "Account cash does not reconcile")
        expected_positions = {}
        for item in state["intents"].values():
            if item is intent or item["phase"] == "DONE":
                size = sum((amount(t["size"]) for t in item["trades"].values()
                            if t["status"] == "CONFIRMED"), Decimal(0))
                if size:
                    expected_positions[item["token_id"]] = expected_positions.get(item["token_id"], Decimal(0)) + size
        positions = {token: amount(size) for token, size in payload["positions"].items() if amount(size)}
        require(positions == expected_positions, "Account positions do not reconcile")
        intent["accounted_cost_micro"] = cost
        intent["phase"] = "DONE"
        state["stream_gap"] = False
    else:
        raise ReplayError("Unknown command")


class ExecutionReplay:
    def __init__(self, path: Path, *, capital_micro: int = 50_000_000,
                 risk_limit_micro: int = 20_000_000, group_limit_micro: int = 10_000_000):
        self.path = Path(path).resolve()
        production = Path(__file__).resolve().parents[1] / ".polycast" / "polycast.sqlite3"
        require(self.path != production.resolve() and not self.path.exists(),
                "Use create() for a NEW isolated database or open() for restart")
        self.config = {"capital_micro": integer(capital_micro), "risk_limit_micro": integer(risk_limit_micro),
                       "group_limit_micro": integer(group_limit_micro)}
        with self.connect() as db:
            db.execute("CREATE TABLE config (value TEXT NOT NULL, digest TEXT NOT NULL, "
                       "schema_version INTEGER NOT NULL, head_seq INTEGER NOT NULL, head_digest TEXT NOT NULL)")
            config_raw = canon(self.config)
            db.execute("INSERT INTO config VALUES (?,?,2,0,?)", (config_raw, hashed(config_raw), hashed(config_raw)))
            db.execute("CREATE TABLE journal (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, "
                       "digest TEXT NOT NULL, request_json TEXT NOT NULL, event_digest TEXT NOT NULL, "
                       "event_json TEXT NOT NULL, prev_digest TEXT NOT NULL, chain_digest TEXT NOT NULL)")

    @classmethod
    def create(cls, path: Path, **options) -> "ExecutionReplay":
        return cls(path, **options)

    @classmethod
    def open(cls, path: Path) -> "ExecutionReplay":
        obj = object.__new__(cls)
        obj.path = Path(path).resolve()
        require(obj.path.name != "polycast.sqlite3", "Production database forbidden")
        require(obj.path.exists(), "Replay database does not exist")
        with obj.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(config)")}
            require(columns == {"value", "digest", "schema_version", "head_seq", "head_digest"},
                    "Legacy replay integrity unverified; no automatic migration")
            obj.config = json.loads(db.execute("SELECT value FROM config").fetchone()[0])
            obj._read(db)
        return obj

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    def _read(self, db) -> dict:
        rows = db.execute("SELECT value,digest,schema_version,head_seq,head_digest FROM config").fetchall()
        require(len(rows) == 1, "Journal configuration count mismatch")
        config_raw, config_hash, version, head_seq, head_digest = rows[0]
        require(version == 2 and hashed(config_raw) == config_hash and config_raw == canon(self.config),
                "Journal configuration integrity mismatch")
        state = {**self.config, "intents": {}, "stream_gap": False, "seq": 0,
                 "journal_integrity_verified": True, "journal_head_sha256": config_hash}
        for seq, event_id, request_hash, request_raw, event_hash, raw, previous, chain in db.execute(
                "SELECT seq,event_id,digest,request_json,event_digest,event_json,prev_digest,chain_digest "
                "FROM journal ORDER BY seq"):
            require(seq == state['seq'] + 1, "Journal sequence integrity mismatch")
            require(hashed(request_raw) == request_hash and hashed(raw) == event_hash,
                    "Journal request/event integrity mismatch")
            require(previous == state['journal_head_sha256'] and
                    chain == chain_hash(seq, event_id, request_hash, event_hash, previous),
                    "Journal chain integrity mismatch")
            try:
                event, request = json.loads(raw), json.loads(request_raw)
                require(canon(event) == raw and canon(request) == request_raw, "Noncanonical journal record")
            except (TypeError, ValueError) as exc:
                raise ReplayError("Malformed journal record") from exc
            apply(state, event)
            state["seq"] = seq
            state["journal_head_sha256"] = chain
        require(state['seq'] == head_seq and state['journal_head_sha256'] == head_digest,
                "Journal committed head mismatch")
        return state

    def snapshot(self) -> dict:
        with self.connect() as db:
            state = self._read(db)
        return {**state, "risk": risk(state)}

    def command(self, event_id: str, kind: str, payload: dict) -> dict:
        text_id(event_id)
        event = {"type": kind, "payload": payload}
        raw = request_raw = canon(event)
        digest = hashed(request_raw)
        failure = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                state = self._read(db)
                old = db.execute("SELECT digest FROM journal WHERE event_id=?", (event_id,)).fetchone()
                if old:
                    require(old[0] == digest, "Event identity reused with different payload")
                    db.commit()
                    return {**state, "risk": risk(state), "duplicate": True}
                if kind == "reconcile":
                    require(payload.get("observed_seq") == state["seq"], "Snapshot predates journal changes")
                candidate = copy.deepcopy(state)
                try:
                    apply(candidate, event)
                except ReconcilePending as exc:
                    event = {"type": "reconcile_deferred", "payload": {
                        "intent_id": payload["intent_id"], "reason": str(exc),
                        **({"observation_sha256": payload["observation_sha256"]}
                           if "observation_sha256" in payload else {})}}
                    raw = canon(event)
                    failure = exc
                except ReplayError as exc:
                    if kind in {"trade", "ack", "reconcile"} and payload.get("intent_id") in state["intents"]:
                        # An invalid external receipt persists a stop, never releases a hold.
                        event = {"type": "fault", "payload": {"intent_id": payload["intent_id"], "reason": str(exc)}}
                        raw = canon(event)
                        failure = exc
                    else:
                        raise
                seq, event_digest = state['seq'] + 1, hashed(raw)
                previous = state['journal_head_sha256']
                linked = chain_hash(seq, event_id, digest, event_digest, previous)
                db.execute("INSERT INTO journal(seq,event_id,digest,request_json,event_digest,event_json,prev_digest,chain_digest) "
                           "VALUES (?,?,?,?,?,?,?,?)", (seq, event_id, digest, request_raw, event_digest, raw, previous, linked))
                db.execute("UPDATE config SET head_seq=?,head_digest=?", (seq, linked))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        if failure:
            raise failure
        return self.snapshot()
