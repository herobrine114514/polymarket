from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .analysis import hashes, make_payload
from .blackout import active_window, evidence_cooling
from .calendar import active_official_window
from .config import Config
from .book_drift import assess_book_drift, book_top, forecast_basis
from .execution import FEE_METHOD, fill_totals, match_levels
from .market import (CHAINLINK_BTC_TWAP, complete_fed_event, fed_outcome_slot,
                     live_market, order_book, parse_utc, _list)
from .provider import active_provider
from .paper_execution import PAPER_EXECUTION_METHOD, PaperExecutionError, sample_fok_execution
from .risk import active_halts, assert_entry_allowed
from .store import balance, digest_text, evidence_for_market, get_market, now_iso


def _risk_group(market: sqlite3.Row) -> str:
    title = (market["event_title"] + " " + market["question"]).lower()
    if any(word in title for word in ("fed", "fomc", "powell", "interest rate")):
        return "fed-policy"
    if any(word in title for word in ("cpi", "inflation", "payroll", "unemployment")):
        return "us-macro-data"
    category = market["category"] if "category" in market.keys() else "macro"
    if category == "crypto":
        return "crypto-hourly"
    if category == "crypto5m":
        return "btc-5m"
    if category == "daily":
        return "mlb-daily"
    return "event:" + market["event_id"]


def _blackout_reason(conn: sqlite3.Connection, market: sqlite3.Row, config: Config) -> str | None:
    scheduled = active_window(config.data_dir / "blackouts.json", _risk_group(market))
    if scheduled:
        return f"Scheduled release blackout: {scheduled}"
    official = active_official_window(conn, _risk_group(market))
    if official:
        return f"Scheduled release blackout: {official}"
    for evidence in evidence_for_market(conn, market):
        if evidence["source_id"] in ("binance_klines", "coinbase_btc_5m",
                                     "chainlink_btc_twap60", "coinbase_exchange_status"):
            continue  # Continuous price observations and venue health checks are the premise of short markets.
        if evidence_cooling(evidence["first_seen_at"], evidence["published_at"],
                            config.evidence_cooldown_minutes):
            return "Fresh relevant evidence is in its cooling window"
    return None


def _prediction_current(conn: sqlite3.Connection, market: sqlite3.Row,
                        prediction: sqlite3.Row, config: Config) -> bool:
    evidence = evidence_for_market(conn, market)
    if not evidence:
        return False
    if market["category"] == "crypto5m":
        now = datetime.now(timezone.utc)
        try:
            review_at = parse_utc(json.loads(prediction["model_output_json"]).get("reviewed_at"))
        except (TypeError, ValueError):
            return False
        relay = next((item for item in evidence
                      if item["source_id"] == "chainlink_btc_twap60"), None)
        observed_at = parse_utc(relay["updated_at"]) if relay else None
        if (review_at is None or observed_at is None or
                not -5 <= (now - review_at).total_seconds() <= 30 or
                not -5 <= (now - observed_at).total_seconds() <= 30):
            return False
    if market["category"] == "crypto":
        binance = next((item for item in evidence
                        if item["source_id"] == "binance_klines"), None)
        observed_at = parse_utc(binance["updated_at"]) if binance else None
        if (observed_at is None or not -5 <=
                (datetime.now(timezone.utc) - observed_at).total_seconds() <= 90):
            return False
    payload = make_payload(dict(market), [dict(item) for item in evidence], now_iso(),
                           active_provider(config.data_dir))
    rules_hash, evidence_hash = hashes(payload, config.analysis_cooldown_hours)
    return (prediction["rules_hash"] == rules_hash and
            prediction["evidence_hash"] == evidence_hash)


def _joint_event_reason(conn: sqlite3.Connection, market: sqlite3.Row,
                        config: Config) -> str | None:
    if fed_outcome_slot(market["question"]) is None:
        return None
    siblings = conn.execute("""SELECT * FROM markets WHERE event_id=? AND in_watchlist=1
        ORDER BY id""", (market["event_id"],)).fetchall()
    if not complete_fed_event([dict(row) for row in siblings]):
        return "Joint Fed outcome set is incomplete"
    joint_ids: set[str] = set()
    joint_maps: set[str] = set()
    probabilities: list[float] = []
    for sibling in siblings:
        prediction = conn.execute("""SELECT * FROM predictions WHERE market_id=?
            ORDER BY as_of DESC,id DESC LIMIT 1""", (sibling["id"],)).fetchone()
        if prediction is None or prediction["abstain"] or prediction["probability"] is None:
            return "Joint Fed forecast is incomplete or abstained"
        try:
            output = json.loads(prediction["model_output_json"])
            joint_id = output.get("joint_forecast_id")
            joint_map = output.get("joint_probabilities")
            if (not isinstance(joint_id, str) or not joint_id or
                    not isinstance(joint_map, dict) or
                    set(joint_map) != {row["id"] for row in siblings} or
                    output.get("joint_event_id") != market["event_id"] or
                    output.get("review", {}).get("approved") is not True or
                    parse_utc(output.get("reviewed_at")) is None or
                    (parse_utc(output.get("valid_until")) or datetime.min.replace(
                        tzinfo=timezone.utc)) <= datetime.now(timezone.utc)):
                return "Joint Fed forecast is not fully reviewed and current"
        except (TypeError, ValueError, AttributeError):
            return "Joint Fed forecast audit is invalid"
        if not _prediction_current(conn, sibling, prediction, config):
            return "Joint Fed forecast evidence or rules changed"
        try:
            expected_probability = float(joint_map[sibling["id"]])
        except (TypeError, ValueError):
            return "Joint Fed forecast audit is invalid"
        if not math.isclose(float(prediction["probability"]), expected_probability,
                            rel_tol=0, abs_tol=1e-8):
            return "Joint Fed probabilities are inconsistent"
        joint_ids.add(joint_id)
        joint_maps.add(digest_text(joint_map))
        probabilities.append(float(prediction["probability"]))
    if len(joint_ids) != 1 or len(joint_maps) != 1 or not math.isclose(sum(probabilities), 1.0, rel_tol=0,
                                               abs_tol=1e-6):
        return "Joint Fed probabilities are inconsistent"
    return None


def _live_matches(saved: sqlite3.Row, live: dict) -> bool:
    tokens = _list(live.get("clobTokenIds"))
    outcomes = _list(live.get("outcomes"))
    return (
        str(live.get("id")) == saved["id"] and
        not live.get("closed") and bool(live.get("active")) and
        bool(live.get("acceptingOrders")) and bool(live.get("enableOrderBook")) and
        len(tokens) == 2 and str(tokens[0]) == saved["yes_token"] and
        str(tokens[1]) == saved["no_token"] and
        (saved["category"] == "macro" or outcomes == [saved["yes_label"], saved["no_label"]]) and
        (saved["category"] != "daily" or parse_utc(live.get("gameStartTime")) ==
         parse_utc(saved["end_at"])) and
        (saved["category"] not in ("crypto", "crypto5m") or parse_utc(live.get("endDate")) ==
         parse_utc(saved["end_at"])) and
        (saved["category"] != "crypto5m" or
         (live.get("resolutionSource") == CHAINLINK_BTC_TWAP and
          (live.get("cryptoMarketConfig") or {}).get("id") == "btc-5m-twap-60" and
          (live.get("cryptoMarketConfig") or {}).get("twapLookbackSeconds") == 60)) and
        (live.get("description") or "") == saved["description"] and
        float((live.get("feeSchedule") or {}).get("rate") or 0) == saved["fee_rate"] and
        (parse_utc(live.get("endDate")) or datetime.max.replace(tzinfo=timezone.utc)) > datetime.now(timezone.utc)
    )


def _quote(market: sqlite3.Row, side: str, shares: float, config: Config) -> dict | None:
    token = market["yes_token"] if side == "YES" else market["no_token"]
    book = order_book(token)
    try:
        book_time = datetime.fromtimestamp(int(book["timestamp"]) / 1000, timezone.utc)
    except (KeyError, ValueError, OverflowError):
        return None
    age_seconds = (datetime.now(timezone.utc) - book_time).total_seconds()
    if age_seconds > config.quote_max_age_seconds or age_seconds < -5:
        return None
    fills = match_levels(book, shares, "asks")
    if fills is None:
        return None
    _, average, fee_total = fill_totals(fills, shares, float(market["fee_rate"]))
    worst = fills[-1]["price"]
    if not (0 < average <= worst < 1):
        return None
    fee = fee_total / shares
    top = book_top(book)
    return {"side": side, "shares": shares, "avg_ask": average,
            "book_bid": top[0] if top else None, "book_ask": top[1] if top else None,
            "worst_ask": worst, "fee_per_share": fee,
            "fee_method": FEE_METHOD, "fills": fills,
            "book_timestamp": book_time.isoformat(timespec="seconds")}


def candidate(conn: sqlite3.Connection, market_id: str, config: Config) -> tuple[dict | None, str]:
    market = get_market(conn, market_id)
    if (parse_utc(market["end_at"]) or datetime.min.replace(tzinfo=timezone.utc)) <= datetime.now(timezone.utc):
        return None, "Market entry cutoff passed"
    halts = active_halts(conn)
    if halts:
        return None, "Paper risk halted: " + ", ".join(row["reason"] for row in halts)
    if conn.execute("SELECT 1 FROM resolutions WHERE market_id=?", (market_id,)).fetchone():
        return None, "Market outcome already recorded"
    prediction = conn.execute("SELECT * FROM predictions WHERE market_id=? ORDER BY as_of DESC,id DESC LIMIT 1",
                              (market_id,)).fetchone()
    if prediction is None or prediction["abstain"]:
        return None, "No actionable prediction"
    try:
        analysis = json.loads(prediction["model_output_json"])
    except (TypeError, json.JSONDecodeError):
        return None, "Prediction audit record is invalid"
    if analysis.get("review", {}).get("approved") is not True:
        return None, "Prediction has no approved independent review"
    if parse_utc(analysis.get("reviewed_at")) is None:
        return None, "Prediction has no review completion time"
    valid_until = parse_utc(analysis.get("valid_until"))
    if valid_until is None or datetime.now(timezone.utc) > valid_until:
        return None, "Prediction validity window ended"
    blackout = _blackout_reason(conn, market, config)
    if blackout:
        return None, blackout
    if not _prediction_current(conn, market, prediction, config):
        return None, "Prediction evidence or review window changed"
    joint_reason = _joint_event_reason(conn, market, config)
    if joint_reason:
        return None, joint_reason
    if conn.execute("SELECT 1 FROM paper_orders WHERE prediction_id=?", (prediction["id"],)).fetchone():
        return None, "Prediction already used"
    if conn.execute("""SELECT 1 FROM trade_intents WHERE prediction_id=?
        AND status IN ('reserved','unknown')""", (prediction["id"],)).fetchone():
        return None, "Prediction already has a reserved or unknown intent"
    forecast_time = parse_utc(prediction["as_of"])
    if forecast_time is None or (datetime.now(timezone.utc) - forecast_time).total_seconds() > config.analysis_cooldown_hours * 3600:
        return None, "Prediction is stale"
    if not _live_matches(market, live_market(market_id)):
        return None, "Market rules, fees, token mapping, or trading state changed; rescan"
    probability = float(prediction["probability"])
    shares = max(5.0, float(market["min_order_size"]))
    raw_quotes = []
    in_band_quotes = []
    basis = forecast_basis(conn, prediction) if market['category'] == 'crypto5m' else None
    drift_rejections = []
    for side in ("YES", "NO"):
        quote = _quote(market, side, shares, config)
        if quote is None:
            continue
        if market['category'] == 'crypto5m':
            audit = assess_book_drift(basis, quote, config)
            conn.execute('''INSERT INTO forecast_book_checks
                (prediction_id,checked_at,stage,side,allowed,audit_json) VALUES(?,?,?,?,?,?)''',
                (prediction['id'], audit['checked_at'], 'candidate', side, int(audit['allowed']),
                 json.dumps(audit, sort_keys=True)))
            conn.commit()  # Do not hold a SQLite writer lock through the next network read.
            if not audit['allowed']:
                drift_rejections.append(audit['reason'])
                continue
            quote['forecast_book_basis'] = basis
            quote['forecast_book_check'] = audit
        p_side = probability if side == "YES" else 1.0 - probability
        cost_per_share = quote["avg_ask"] + quote["fee_per_share"]
        total_cost = shares * cost_per_share
        edge = p_side - config.probability_haircut - cost_per_share
        expected_roi = edge / cost_per_share if cost_per_share > 0 else 0.0
        quote["expected_roi"] = expected_roi
        quote["expected_edge"] = edge
        quote["total_cost"] = total_cost
        raw_quotes.append(quote)
        if config.min_price <= quote["avg_ask"] <= config.max_price:
            in_band_quotes.append(quote)
    if not raw_quotes:
        if drift_rejections:
            return None, 'BTC5m forecast basis invalidated: ' + '; '.join(dict.fromkeys(drift_rejections))
        return None, "No fresh, sufficiently deep order book"
    if not in_band_quotes:
        best_raw = max(raw_quotes, key=lambda quote: quote["expected_edge"])
        return None, f"Best ask {best_raw['avg_ask']:.4f} outside tradable price band [{config.min_price}, {config.max_price}]"
    best = max(in_band_quotes, key=lambda quote: quote["expected_edge"])
    if best["expected_edge"] < config.min_edge or best.get("expected_roi", 0.0) < config.min_roi:
        return None, f"Best conservative net edge {best['expected_edge']:.4f} is below threshold"
    return {**best, "market_id": market_id, "event_id": market["event_id"],
            "risk_group": _risk_group(market), "prediction_id": int(prediction["id"]),
            "captured_at": now_iso()}, "candidate"


def _intent_event(conn: sqlite3.Connection, intent_id: int, before: str | None,
                  after: str, reason: str) -> None:
    conn.execute("""INSERT INTO trade_intent_events(intent_id,at,from_status,to_status,reason)
        VALUES(?,?,?,?,?)""", (intent_id, now_iso(), before, after, reason))


def expire_paper_intents(conn: sqlite3.Connection) -> int:
    """A paper reservation has no external submission, so it can expire safely."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute("""SELECT id FROM trade_intents
            WHERE status='reserved' AND expires_at<=?""", (now_iso(),)).fetchall()
        for row in rows:
            conn.execute("""UPDATE trade_intents SET status='expired',updated_at=?,
                failure_reason='Paper quote expired without a fill' WHERE id=?""",
                (now_iso(), row["id"]))
            _intent_event(conn, row["id"], "reserved", "expired", "Quote expired")
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise


def _validated_order(conn: sqlite3.Connection, quote: dict, config: Config,
                     exclude_intent_id: int = 0) -> tuple[sqlite3.Row, float, float, float]:
    market = get_market(conn, quote["market_id"])
    if (parse_utc(market["end_at"]) or datetime.min.replace(tzinfo=timezone.utc)) <= datetime.now(timezone.utc):
        raise ValueError("Market entry cutoff passed")
    assert_entry_allowed(conn, config)
    if conn.execute("SELECT 1 FROM resolutions WHERE market_id=?", (quote["market_id"],)).fetchone():
        raise ValueError("Market outcome already recorded")
    blackout = _blackout_reason(conn, market, config)
    if blackout:
        raise ValueError(blackout)
    latest = conn.execute("SELECT * FROM predictions WHERE market_id=? ORDER BY as_of DESC,id DESC LIMIT 1",
                          (quote["market_id"],)).fetchone()
    if latest is None or int(latest["id"]) != quote["prediction_id"]:
        raise ValueError("Prediction was superseded")
    if latest["abstain"]:
        raise ValueError("Prediction abstained")
    analysis = json.loads(latest["model_output_json"])
    if analysis.get("review", {}).get("approved") is not True:
        raise ValueError("Prediction has no approved independent review")
    if parse_utc(analysis.get("reviewed_at")) is None:
        raise ValueError("Prediction has no review completion time")
    valid_until = parse_utc(analysis.get("valid_until"))
    if valid_until is None or datetime.now(timezone.utc) > valid_until:
        raise ValueError("Prediction validity window ended")
    if not _prediction_current(conn, market, latest, config):
        raise ValueError("Prediction evidence or review window changed")
    joint_reason = _joint_event_reason(conn, market, config)
    if joint_reason:
        raise ValueError(joint_reason)
    if conn.execute("SELECT 1 FROM paper_orders WHERE prediction_id=?", (quote["prediction_id"],)).fetchone():
        raise ValueError("Prediction already used")
    captured_at = parse_utc(quote["captured_at"])
    book_time = parse_utc(quote["book_timestamp"])
    now = datetime.now(timezone.utc)
    if (captured_at is None or book_time is None or
            not -5 <= (now - captured_at).total_seconds() <= config.quote_max_age_seconds or
            not -5 <= (now - book_time).total_seconds() <= config.quote_max_age_seconds):
        raise ValueError("Quote expired")
    if quote["event_id"] != market["event_id"] or quote["risk_group"] != _risk_group(market):
        raise ValueError("Quote event or risk group mismatch")
    if quote["side"] not in ("YES", "NO"):
        raise ValueError("Invalid quote side")
    shares, average, worst = (float(quote[key]) for key in ("shares", "avg_ask", "worst_ask"))
    if (not all(math.isfinite(x) for x in (shares, average, worst)) or
            shares < market["min_order_size"] or
            not 0 < average <= worst < 1):
        raise ValueError("Invalid order size or price")
    if not (config.min_price <= average <= config.max_price):
        raise ValueError(f"Quote ask {average:.4f} outside tradable price band [{config.min_price}, {config.max_price}]")
    if quote.get("fee_method") != FEE_METHOD:
        raise ValueError("Quote has no supported fill and fee audit")
    try:
        gross, fill_average, fee_total = fill_totals(quote.get("fills"), shares, float(market["fee_rate"]))
        fill_worst = max(float(item["price"]) for item in quote["fills"])
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid quote fill audit") from exc
    if (not math.isclose(average, fill_average, rel_tol=0, abs_tol=1e-8) or
            not math.isclose(worst, fill_worst, rel_tol=0, abs_tol=1e-8)):
        raise ValueError("Quote fill prices mismatch")
    fee = fee_total / shares
    cost = shares * (average + fee)
    cost_per_share = average + fee
    probability = float(latest["probability"])
    p_side = probability if quote["side"] == "YES" else 1.0 - probability
    edge = p_side - config.probability_haircut - cost_per_share
    legacy_edge_005 = p_side - 0.05 - cost_per_share
    raw_edge = p_side - cost_per_share

    quote_edge = float(quote["expected_edge"])
    valid_edge = (
        math.isclose(quote_edge, edge, rel_tol=0, abs_tol=1e-8) or
        math.isclose(quote_edge, legacy_edge_005, rel_tol=0, abs_tol=1e-8) or
        math.isclose(quote_edge, raw_edge, rel_tol=0, abs_tol=1e-8)
    )
    if (not math.isclose(float(quote["fee_per_share"]), fee, rel_tol=0, abs_tol=1e-8) or
            not math.isclose(float(quote["total_cost"]), cost, rel_tol=0, abs_tol=1e-8) or
            not valid_edge):
        raise ValueError("Quote fees, cost, or edge mismatch")
    if edge < config.min_edge:
        raise ValueError("Quote edge is below threshold")
    expected_roi = edge / cost_per_share
    if quote.get("expected_roi") is not None:
        quoted_roi = float(quote["expected_roi"])
        if not math.isfinite(quoted_roi) or not math.isclose(quoted_roi, expected_roi, rel_tol=0, abs_tol=1e-8):
            raise ValueError("Quote ROI mismatch")
    if expected_roi < config.min_roi:
        raise ValueError("Quote ROI is below threshold")
    if market['category'] == 'crypto5m':
        basis = forecast_basis(conn, latest)
        if basis is None or digest_text(quote.get('forecast_book_basis')) != digest_text(basis):
            raise ValueError('Quote does not match immutable forecast book basis')
        drift = assess_book_drift(basis, quote, config)
        if not drift['allowed']:
            raise ValueError('BTC5m forecast basis invalidated: ' + drift['reason'])
    open_risk = conn.execute("""SELECT
        COALESCE(SUM(total_cost),0) total,
        COALESCE(SUM(CASE WHEN market_id=? THEN total_cost ELSE 0 END),0) market_cost,
        COALESCE(SUM(CASE WHEN risk_group=? THEN total_cost ELSE 0 END),0) group_cost
        FROM paper_orders WHERE status='open'""",
        (quote["market_id"], quote["risk_group"])).fetchone()
    reserved = conn.execute("""SELECT
        COALESCE(SUM(max_total_cost),0) total,
        COALESCE(SUM(CASE WHEN market_id=? THEN max_total_cost ELSE 0 END),0) market_cost,
        COALESCE(SUM(CASE WHEN risk_group=? THEN max_total_cost ELSE 0 END),0) group_cost
        FROM trade_intents WHERE status IN ('reserved','unknown') AND id<>?""",
        (quote["market_id"], quote["risk_group"], exclude_intent_id)).fetchone()
    if open_risk["market_cost"] + reserved["market_cost"] + cost > config.initial_cash * config.max_market_fraction + 1e-9:
        raise ValueError("Market risk limit")
    if open_risk["group_cost"] + reserved["group_cost"] + cost > config.initial_cash * config.max_event_fraction + 1e-9:
        raise ValueError("Event group risk limit")
    if open_risk["total"] + reserved["total"] + cost > config.initial_cash * config.max_total_fraction + 1e-9:
        raise ValueError("Total exposure limit")
    cash = balance(conn, config.initial_cash)["cash_unreserved"]
    if exclude_intent_id:
        own = conn.execute("SELECT max_total_cost FROM trade_intents WHERE id=?",
                           (exclude_intent_id,)).fetchone()
        cash += float(own["max_total_cost"]) if own else 0.0
    if cost > cash + 1e-9:
        raise ValueError("Insufficient unreserved paper cash")
    return market, fee, cost, quote_edge


def prepare_paper_intent(conn: sqlite3.Connection, quote: dict, config: Config) -> int:
    """Persist an FOK paper intention and reserve its maximum cost before filling."""
    quote_hash = digest_text(quote)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute("""SELECT id,status,quote_hash,expires_at FROM trade_intents
            WHERE prediction_id=? AND status IN ('reserved','filled','unknown')""",
            (quote["prediction_id"],)).fetchone()
        if existing:
            if existing["status"] == "filled":
                raise ValueError("Prediction already used")
            if (existing["status"] == "reserved" and existing["quote_hash"] == quote_hash and
                    existing["expires_at"] > now_iso()):
                conn.commit()
                return int(existing["id"])
            raise ValueError("Prediction already has a reserved or unknown intent")
        _validated_order(conn, quote, config)
        captured = parse_utc(quote["captured_at"])
        book_time = parse_utc(quote["book_timestamp"])
        expires = min(captured, book_time) + timedelta(seconds=config.quote_max_age_seconds)
        timestamp = now_iso()
        cursor = conn.execute("""INSERT INTO trade_intents
            (prediction_id,market_id,event_id,risk_group,side,shares,limit_price,
             max_total_cost,expected_edge,time_in_force,quote_json,quote_hash,status,
             created_at,updated_at,expires_at)
            VALUES(?,?,?,?,?,?,?,?,?,'FOK',?,?,'reserved',?,?,?)""",
            (quote["prediction_id"], quote["market_id"], quote["event_id"], quote["risk_group"],
             quote["side"], quote["shares"], quote["worst_ask"], quote["total_cost"],
             quote["expected_edge"], json.dumps(quote, ensure_ascii=False, sort_keys=True),
             quote_hash, timestamp, timestamp, expires.astimezone(timezone.utc).isoformat(timespec="seconds")))
        intent_id = int(cursor.lastrowid)
        _intent_event(conn, intent_id, None, "reserved", "Validated FOK paper intention")
        conn.commit()
        return intent_id
    except Exception:
        conn.rollback()
        raise


def refresh_execution(market: dict, quote: dict, config: Config) -> tuple[dict,dict]:
    live = live_market(market["id"])
    if not _live_matches(market,live):
        raise ValueError("Market rules or trading state changed before delayed execution")
    return sample_fok_execution(market,quote,config,live)


def _save_execution_audit(conn: sqlite3.Connection, intent_id: int, audit: dict,
                         execution_quote: dict | None) -> None:
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM paper_execution_audits WHERE intent_id=?",(intent_id,)).fetchone():
            raise ValueError("Paper execution audit already recorded")
        conn.execute("""INSERT INTO paper_execution_audits
            (intent_id,method,captured_at,result,audit_json,audit_hash,
             execution_quote_json,execution_quote_hash) VALUES(?,?,?,?,?,?,?,?)""",
            (intent_id,PAPER_EXECUTION_METHOD,now_iso(),"fillable" if execution_quote else "rejected",
             json.dumps(audit,sort_keys=True),digest_text(audit),
             json.dumps(execution_quote,sort_keys=True) if execution_quote else None,
             digest_text(execution_quote) if execution_quote else None))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def fill_paper_intent(conn: sqlite3.Connection, intent_id: int, config: Config) -> int:
    """Atomically fill from an immutable delayed snapshot within original caps."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        intent = conn.execute("SELECT * FROM trade_intents WHERE id=?", (intent_id,)).fetchone()
        if intent is None or intent["status"] != "reserved":
            raise ValueError("Paper intent is not reserved")
        if intent["expires_at"] <= now_iso():
            raise ValueError("Quote expired")
        frozen = json.loads(intent["quote_json"])
        if digest_text(frozen) != intent["quote_hash"]:
            raise ValueError("Paper intent quote checksum mismatch")
        market, _, original_cost, original_edge = _validated_order(conn, frozen, config, intent_id)
        receipt = conn.execute("SELECT * FROM paper_execution_audits WHERE intent_id=?",(intent_id,)).fetchone()
        if receipt is None or receipt["method"] != PAPER_EXECUTION_METHOD or receipt["result"] != "fillable":
            raise ValueError("Paper intent has no delayed execution audit")
        audit = json.loads(receipt["audit_json"])
        quote = json.loads(receipt["execution_quote_json"])
        if (digest_text(audit) != receipt["audit_hash"] or
                digest_text(quote) != receipt["execution_quote_hash"] or
                audit.get("frozen_quote_sha256") != intent["quote_hash"] or
                audit.get("execution_quote_sha256") != receipt["execution_quote_hash"]):
            raise ValueError("Delayed execution audit checksum mismatch")
        if (quote["market_id"] != frozen["market_id"] or quote["prediction_id"] != frozen["prediction_id"] or
                quote["side"] != frozen["side"] or quote["shares"] != frozen["shares"] or
                quote["worst_ask"] > intent["limit_price"] + 1e-12):
            raise ValueError("Delayed execution changed frozen order terms")
        market, fee, cost, edge = _validated_order(conn, quote, config, intent_id)
        if (intent["market_id"] != market["id"] or
                intent["prediction_id"] != quote["prediction_id"] or
                intent["event_id"] != quote["event_id"] or
                intent["risk_group"] != quote["risk_group"] or
                intent["side"] != quote["side"] or intent["time_in_force"] != "FOK" or
                not math.isclose(intent["shares"], quote["shares"], rel_tol=0, abs_tol=1e-8) or
                not math.isclose(intent["limit_price"], frozen["worst_ask"], rel_tol=0, abs_tol=1e-8) or
                not math.isclose(intent["max_total_cost"], original_cost, rel_tol=0, abs_tol=1e-8) or
                not math.isclose(intent["expected_edge"], original_edge, rel_tol=0, abs_tol=1e-8) or
                cost > intent["max_total_cost"] + 1e-8):
            raise ValueError("Paper intent no longer matches the validated quote")
        conn.execute("""INSERT INTO quotes(market_id,side,captured_at,book_timestamp,shares,
            avg_ask,worst_ask,fee_per_share) VALUES(?,?,?,?,?,?,?,?)""",
            (quote["market_id"], quote["side"], quote["captured_at"], quote["book_timestamp"],
             quote["shares"], quote["avg_ask"], quote["worst_ask"], fee))
        cursor = conn.execute("""INSERT INTO paper_orders(prediction_id,market_id,event_id,risk_group,side,
            created_at,shares,avg_price,fee,total_cost,expected_edge,intent_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (quote["prediction_id"], quote["market_id"], quote["event_id"], quote["risk_group"],
             quote["side"], now_iso(), quote["shares"], quote["avg_ask"],
             quote["shares"] * fee, cost, edge, intent_id))
        conn.execute("""UPDATE trade_intents SET status='filled',updated_at=?
            WHERE id=? AND status='reserved'""", (now_iso(), intent_id))
        _intent_event(conn, intent_id, "reserved", "filled", "Atomic delayed snapshot paper FOK fill")
        conn.commit()
        return int(cursor.lastrowid)
    except Exception:
        conn.rollback()
        raise


def _reject_paper_intent(conn: sqlite3.Connection, intent_id: int, reason: str) -> None:
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        status = "expired" if "expired" in reason.lower() else "rejected"
        cursor = conn.execute("""UPDATE trade_intents SET status=?,updated_at=?,failure_reason=?
            WHERE id=? AND status='reserved'""", (status, now_iso(), reason[:250], intent_id))
        if cursor.rowcount:
            _intent_event(conn, intent_id, "reserved", status, reason[:250])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def place(conn: sqlite3.Connection, quote: dict, config: Config) -> int:
    """Reserve, then complete a paper-only FOK fill; never sends an external order."""
    expire_paper_intents(conn)
    intent_id = prepare_paper_intent(conn, quote, config)
    execution_quote = None
    audit = {"method":PAPER_EXECUTION_METHOD,"frozen_quote_sha256":digest_text(quote),
             "external_orders_submitted":0}
    try:
        market = dict(get_market(conn,quote["market_id"]))
        _validated_order(conn,quote,config,intent_id)
        conn.commit()  # Never hold a SQLite write transaction across HTTP or waiting.
        execution_quote,audit = refresh_execution(market,quote,config)
        _save_execution_audit(conn,intent_id,audit,execution_quote)
        return fill_paper_intent(conn, intent_id, config)
    except (ValueError,RuntimeError) as exc:
        if isinstance(exc,PaperExecutionError):
            audit = exc.audit
        audit["failure_reason"] = str(exc)[:250]
        if not conn.execute("SELECT 1 FROM paper_execution_audits WHERE intent_id=?",(intent_id,)).fetchone():
            _save_execution_audit(conn,intent_id,audit,None)
        _reject_paper_intent(conn, intent_id, str(exc))
        raise


def settle(conn: sqlite3.Connection, market_id: str, winning_side: str,
           source_url: str, resolved_at: str) -> int:
    if winning_side not in ("YES", "NO"):
        raise ValueError("winning side must be YES or NO")
    parsed_url = urlparse(source_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError("A verified HTTPS resolution source is required")
    try:
        resolution_time = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("resolved_at must be a timezone-aware ISO timestamp") from exc
    if resolution_time.tzinfo is None or resolution_time > datetime.now(timezone.utc):
        raise ValueError("resolved_at must be timezone-aware and not in the future")
    resolved_at = resolution_time.astimezone(timezone.utc).isoformat(timespec="seconds")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not conn.execute("SELECT 1 FROM markets WHERE id=?", (market_id,)).fetchone():
            raise KeyError(f"Unknown market {market_id}")
        prior = conn.execute("SELECT * FROM resolutions WHERE market_id=?", (market_id,)).fetchone()
        if prior:
            if (prior["winning_side"], prior["source_url"], prior["resolved_at"]) == (
                    winning_side, source_url, resolved_at):
                conn.commit()
                return 0
            raise ValueError("Conflicting recorded outcome; investigate before changing it")
        conn.execute("""INSERT INTO resolutions(market_id,winning_side,source_url,resolved_at,recorded_at)
            VALUES(?,?,?,?,?)""", (market_id, winning_side, source_url, resolved_at, now_iso()))
        cursor = conn.execute("""UPDATE paper_orders SET status='settled',
            settlement=CASE WHEN side=? THEN 1.0 ELSE 0.0 END
            WHERE market_id=? AND status='open'""", (winning_side, market_id))
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise
