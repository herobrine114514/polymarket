from __future__ import annotations

import argparse
import atexit
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analysis import (BTC5M_PROMPT_VERSION, CODEX_MODEL, PROMPT_VERSION, ProviderUnavailable, event_hashes,
                       hashes, make_event_payload, make_payload, run_codex,
                       run_event_codex, run_event_review, run_review)
from .anchor_audit import audit_btc5m_anchors
from .billing import capture_usage, save_model_usage
from .calendar import calendar_due, refresh_calendars
from .config import Config, load_runtime_settings, save_runtime_settings
from .collector_env import node_collector_env
from .evidence import collect_configured, collect_official
from .market import book_baseline, complete_fed_event, discover, discover_btc_5m, fed_outcome_slot, order_book, parse_utc
from .paper import candidate, expire_paper_intents, place, settle
from .provider import (DEEPSEEK_MODEL, active_provider, has_deepseek_key,
                       save_deepseek_key, select_provider)
from .risk import active_halts, mark_account, resume_after_reconciliation
from .runner import local_cycle_lock, run_recorded
from .short_cycle import collect_short, hourly_window_ready, reconcile_short
from .store import (analysis_market_baseline, balance, database, evidence_for_market, get_market,
                    digest_text, finish_model_call, insert_evidence, latest_analysis_draft,
                    model_calls_today, model_pause_until, now_iso,
                    reserve_model_call, save_analysis_draft, save_analysis_market_baseline,
                    save_market_baseline,
                    save_prediction, sync_news_sources, upsert_markets,
                    withdraw_evidence)


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def scan(config: Config, *, errors: list[str] | None = None) -> list[dict]:
    markets = discover(limit=20, min_liquidity=config.min_liquidity, errors=errors)
    with database(config.database) as conn:
        # Keep cached sectors visible after a discovery outage. The cycle only
        # analyzes the newly verified list, never these unrefreshed rows.
        upsert_markets(conn, markets, reset_watchlist=not bool(errors))
    return markets


def collect(config: Config) -> dict:
    items, errors = collect_official()
    from .social import collect_fed_bluesky
    social_items, social_errors = collect_fed_bluesky()
    items.extend(social_items)
    errors.extend(social_errors)
    extra_items, extra_errors, news_sources = collect_configured(config.data_dir / "sources.json")
    items.extend(extra_items)
    errors.extend(extra_errors)
    with database(config.database) as conn:
        sync_news_sources(conn, news_sources)
        inserted = insert_evidence(conn, items)
        short_markets = [dict(row) for row in conn.execute(
            "SELECT * FROM markets WHERE in_watchlist=1 AND category IN ('crypto','crypto5m','daily')")]
        short = collect_short(conn, short_markets, config.data_dir / "chainlink_twap60.jsonl")
    return {"fetched": len(items) + short["fetched"],
            "new_versions": inserted + short["new_versions"],
            "errors": errors + short["errors"]}


def _budgeted_model_call(config: Config, market_id: str, stage: str, operation,
                         provider: str = "codex"):
    model = CODEX_MODEL if provider == "codex" else DEEPSEEK_MODEL
    with database(config.database) as conn:
        call_id = reserve_model_call(conn, market_id, stage, model,
                                     config.model_call_limit(provider), provider)
    try:
        def save_usage(item):
            with database(config.database) as usage_conn:
                save_model_usage(usage_conn, call_id, item)
        with capture_usage(save_usage):
            result = operation()
    except Exception as exc:
        with database(config.database) as conn:
            pause = exc.retry_at.isoformat(timespec="seconds") if isinstance(exc, ProviderUnavailable) else None
            finish_model_call(conn, call_id, f"{type(exc).__name__}: {exc}", pause, provider)
        raise
    with database(config.database) as conn:
        finish_model_call(conn, call_id, provider=provider)
    return result


def _invoke_model(operation, payload: dict, config: Config, *extra):
    if payload.get("provider", "codex") == "codex":
        return operation(payload, *extra)
    return operation(payload, *extra, data_dir=config.data_dir)


def _capture_market_baseline(config: Config, market_id: str, prediction_id: int) -> dict:
    with database(config.database) as conn:
        market = get_market(conn, market_id)
        token = market["yes_token"]
    snapshot = book_baseline(order_book(token), config.quote_max_age_seconds)
    if snapshot is None:
        return {"status": "unavailable", "reason": "No fresh two-sided YES book"}
    book_timestamp, bid, ask = snapshot
    with database(config.database) as conn:
        save_market_baseline(conn, prediction_id,
                             datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                             book_timestamp, bid, ask)
    return {"status": "captured", "p_yes_mid": round((bid + ask) / 2, 6),
            "spread": round(ask - bid, 6)}


def _pre_analysis_market_baseline(config: Config, yes_token: str) -> dict:
    # The comparison book must exist before the AI sees any input.
    snapshot = book_baseline(order_book(yes_token), min(config.quote_max_age_seconds, 10))
    if snapshot is None:
        return {"status": "unavailable", "reason": "No fresh two-sided YES book before analysis"}
    book_timestamp, bid, ask = snapshot
    return {"status": "captured", "captured_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "book_timestamp": book_timestamp, "bid": bid, "ask": ask,
            "p_yes_mid": round((bid + ask) / 2, 6),
            "spread": round(ask - bid, 6)}


def analyze(config: Config, market_id: str, *, once_per_window: bool = False) -> dict:
    with database(config.database) as conn:
        category = get_market(conn, market_id)["category"]
    if category != "crypto5m":
        return _analyze_unlocked(config, market_id)
    # The pre-call claim check alone races between the scheduled cycle and
    # five-minute worker. Hold an OS lock through analysis, review and saving.
    with local_cycle_lock(config.data_dir / "btc5m-analysis.lock"):
        if once_per_window:
            if not config.five_minute_auto_enabled():
                return {"market_id": market_id, "status": "paused",
                        "reason": "Automatic BTC 5m predictions are paused"}
            with database(config.database) as conn:
                previous = conn.execute("""SELECT id,as_of FROM predictions
                    WHERE market_id=? ORDER BY id DESC LIMIT 1""", (market_id,)).fetchone()
            if previous:
                return {"market_id": market_id, "prediction_id": previous["id"],
                        "as_of": previous["as_of"], "status": "already_analyzed"}
        return _analyze_unlocked(config, market_id)


def _analyze_unlocked(config: Config, market_id: str) -> dict:
    provider = active_provider(config.data_dir)
    with database(config.database) as conn:
        market = dict(get_market(conn, market_id))
        siblings = [dict(row) for row in conn.execute("""SELECT * FROM markets
            WHERE event_id=? AND in_watchlist=1 ORDER BY id""", (market["event_id"],))]
    if complete_fed_event(siblings):
        return analyze_event(config, siblings)
    with database(config.database) as conn:
        market = dict(get_market(conn, market_id))
        if market["category"] == "crypto":
            if not hourly_window_ready(market, datetime.now(timezone.utc)):
                raise ValueError("Hourly analysis requires its first complete minute and an open window")
            refreshed = collect_short(conn, [market], config.data_dir / "chainlink_twap60.jsonl")
            if refreshed["errors"]:
                raise RuntimeError("Hourly Binance snapshot refresh failed")
        if market["category"] == "crypto5m":
            start = datetime.fromtimestamp(int(market["source_ref"].split(":")[1]), timezone.utc)
            end = datetime.fromisoformat(market["end_at"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if not start + timedelta(seconds=20) <= now < end - timedelta(seconds=90):
                raise ValueError("BTC 5m analysis must start after opening and with 90s remaining")
        if conn.execute("SELECT 1 FROM resolutions WHERE market_id=?", (market_id,)).fetchone():
            raise ValueError("Market outcome already recorded")
        evidence = [dict(item) for item in evidence_for_market(conn, market)]
        if not evidence:
            raise ValueError("No relevant evidence is available for this market")
        if market["category"] == "crypto":
            snapshot = next((item for item in evidence
                             if item["source_id"] == "binance_klines"), None)
            observed_at = parse_utc(snapshot["updated_at"]) if snapshot else None
            if (observed_at is None or not -5 <=
                    (datetime.now(timezone.utc) - observed_at).total_seconds() <= 90):
                raise ValueError("Hourly Binance snapshot is stale")
        if (market["category"] == "crypto5m" and
                not any(item["source_id"] == "chainlink_btc_twap60" for item in evidence)):
            raise ValueError("BTC 5m requires a fresh Chainlink TWAP opening snapshot")
    pre_baseline = None
    if market["category"] in ("crypto5m", "crypto", "daily"):
        try:
            pre_baseline = _pre_analysis_market_baseline(config, market["yes_token"])
        except Exception as exc:
            pre_baseline = {"status": "unavailable", "reason": f"{type(exc).__name__} before analysis"}
    payload = make_payload(market, evidence,
                           pre_baseline["captured_at"] if pre_baseline and
                           pre_baseline["status"] == "captured" else
                           datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                           provider, market_baseline=pre_baseline)
    rules_hash, evidence_hash = hashes(payload, config.analysis_cooldown_hours)
    with database(config.database) as conn:
        old = conn.execute("""SELECT id,as_of FROM predictions
            WHERE market_id=? AND rules_hash=? AND evidence_hash=?""",
            (market_id, rules_hash, evidence_hash)).fetchone()
        draft = latest_analysis_draft(conn, market_id, rules_hash, evidence_hash)
    if old:
        return {"market_id": market_id, "prediction_id": old["id"],
                "as_of": old["as_of"], "status": "unchanged"}
    if draft:
        draft_payload = json.loads(draft["payload_json"])
        draft_time = datetime.fromisoformat(draft_payload["as_of"])
        if datetime.now(timezone.utc) - draft_time <= timedelta(hours=config.analysis_cooldown_hours):
            payload = draft_payload
            result = json.loads(draft["result_json"])
        else:
            draft = None
    if not draft:
        if pre_baseline and pre_baseline["status"] == "captured":
            with database(config.database) as conn:
                save_analysis_market_baseline(conn, market_id, rules_hash, evidence_hash,
                    payload["as_of"], pre_baseline["captured_at"],
                    pre_baseline["book_timestamp"], pre_baseline["bid"], pre_baseline["ask"])
        result = _budgeted_model_call(config, market_id, "analysis",
                                      lambda: _invoke_model(run_codex, payload, config), provider)
        with database(config.database) as conn:
            save_analysis_draft(conn, market_id, rules_hash, evidence_hash, payload, result)
    if market["category"] == "crypto5m":
        review = run_review(payload, result)
    else:
        review = _budgeted_model_call(config, market_id, "review",
                                      lambda: _invoke_model(run_review, payload, config, result), provider)
    if market["category"] == "crypto5m" and datetime.now(timezone.utc) >= end - timedelta(seconds=20):
        raise RuntimeError("BTC 5m review finished too close to market end")
    result["analysis_abstain"] = bool(result["abstain"])
    result["review"] = review
    result["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    result["prompt_version"] = (BTC5M_PROMPT_VERSION if market["category"] == "crypto5m"
                                else PROMPT_VERSION)
    result["model_version"] = CODEX_MODEL if provider == "codex" else DEEPSEEK_MODEL
    result["provider"] = provider
    result["valid_until"] = (datetime.fromisoformat(payload["as_of"]) +
                             timedelta(hours=config.analysis_cooldown_hours)).isoformat(timespec="seconds")
    if not review["approved"]:
        result["abstain"] = True
    with database(config.database) as conn:
        prediction_id = save_prediction(conn, market_id, rules_hash, evidence_hash, result)
    if market["category"] in ("crypto5m", "crypto", "daily"):
        with database(config.database) as conn:
            staged = analysis_market_baseline(conn, market_id, rules_hash,
                                              evidence_hash, payload["as_of"])
            saved_as_of = conn.execute("SELECT as_of FROM predictions WHERE id=?",
                                       (prediction_id,)).fetchone()["as_of"]
            if staged and saved_as_of == payload["as_of"]:
                save_market_baseline(conn, prediction_id, staged["captured_at"],
                                     staged["book_timestamp"], staged["yes_best_bid"],
                                     staged["yes_best_ask"])
                baseline = {"status": "captured", "phase": "pre_analysis",
                            "p_yes_mid": round((staged["yes_best_bid"] + staged["yes_best_ask"]) / 2, 6),
                            "spread": round(staged["yes_best_ask"] - staged["yes_best_bid"], 6)}
            else:
                baseline = {"status": "unavailable", "reason": "No contemporaneous pre-analysis book"}
    else:
        try:
            baseline = _capture_market_baseline(config, market_id, prediction_id)
        except Exception as exc:
            baseline = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"[:160]}
    return {"market_id": market_id, "prediction_id": prediction_id,
            "status": "analyzed", "p_yes": None if result["abstain"] else result["p_yes"],
            "abstain": result["abstain"], "reasoning": result["reasoning"],
            "market_baseline": baseline,
            "review_approved": review["approved"], "review_reason": review["reason"]}


def analyze_event(config: Config, markets: list[dict]) -> dict:
    provider = active_provider(config.data_dir)
    if not complete_fed_event(markets):
        raise ValueError("Cannot analyze an incomplete Fed outcome event")
    with database(config.database) as conn:
        rows = [dict(get_market(conn, market["id"])) for market in markets]
        if any(conn.execute("SELECT 1 FROM resolutions WHERE market_id=?", (row["id"],)).fetchone()
               for row in rows):
            raise ValueError("An event outcome is already recorded")
        evidence = [dict(item) for item in evidence_for_market(conn, get_market(conn, rows[0]["id"]))]
        if not evidence:
            raise ValueError("No relevant evidence is available for this event")
    pre_baselines = {}
    for row in rows:
        try:
            pre_baselines[row["id"]] = _pre_analysis_market_baseline(config, row["yes_token"])
        except Exception as exc:
            pre_baselines[row["id"]] = {"status": "unavailable",
                                         "reason": f"{type(exc).__name__} before analysis"}
    payload = make_event_payload(rows, evidence, now_iso(), provider)
    with database(config.database) as conn:
        rules_hash, evidence_hash = event_hashes(payload, config.analysis_cooldown_hours)
        primary_id = payload["markets"][0]["market_id"]
        old = []
        for row in rows:
            own_rules, own_evidence = hashes(make_payload(row, evidence, payload["as_of"], provider),
                                             config.analysis_cooldown_hours)
            existing = conn.execute("""SELECT id,abstain,probability FROM predictions
                WHERE market_id=? AND rules_hash=? AND evidence_hash=?""",
                (row["id"], own_rules, own_evidence)).fetchone()
            old.append(dict(existing) if existing else None)
        draft = latest_analysis_draft(conn, primary_id, rules_hash, evidence_hash)
    if all(old):
        return {"event_id": payload["event_id"], "status": "unchanged",
                "predictions": [{"market_id": row["id"], "prediction_id": prior["id"],
                                 "abstain": bool(prior["abstain"]), "p_yes": prior["probability"]}
                                for row, prior in zip(rows, old)]}
    if draft:
        draft_payload = json.loads(draft["payload_json"])
        draft_time = datetime.fromisoformat(draft_payload["as_of"])
        if datetime.now(timezone.utc) - draft_time <= timedelta(hours=config.analysis_cooldown_hours):
            payload = draft_payload
            result = json.loads(draft["result_json"])
        else:
            draft = None
    if not draft:
        with database(config.database) as conn:
            for row in rows:
                snapshot = pre_baselines[row["id"]]
                if snapshot["status"] != "captured":
                    continue
                own_rules, own_evidence = hashes(make_payload(row, evidence, payload["as_of"], provider),
                                                 config.analysis_cooldown_hours)
                if 0 <= (datetime.fromisoformat(payload["as_of"]) -
                         datetime.fromisoformat(snapshot["captured_at"])).total_seconds() <= 10:
                    save_analysis_market_baseline(conn, row["id"], own_rules, own_evidence,
                        payload["as_of"], snapshot["captured_at"], snapshot["book_timestamp"],
                        snapshot["bid"], snapshot["ask"])
        result = _budgeted_model_call(config, primary_id, "analysis",
                                      lambda: _invoke_model(run_event_codex, payload, config), provider)
        with database(config.database) as conn:
            save_analysis_draft(conn, primary_id, rules_hash, evidence_hash, payload, result)
    review = _budgeted_model_call(config, primary_id, "review",
                                  lambda: _invoke_model(run_event_review, payload, config, result), provider)
    reviewed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    joint_id = digest_text({"event_id": payload["event_id"], "as_of": payload["as_of"],
                            "probabilities": result["probabilities"],
                            "rules_hash": rules_hash, "evidence_hash": evidence_hash})
    predictions = []
    with database(config.database) as conn:
        for row in rows:
            own_rules, own_evidence = hashes(make_payload(row, evidence, payload["as_of"], provider),
                                             config.analysis_cooldown_hours)
            output = {"market_id": row["id"], "as_of": payload["as_of"],
                      "p_yes": result["probabilities"][row["id"]],
                      "abstain": result["abstain"] or not review["approved"],
                      "reasoning": result["reasoning"],
                      "assumptions": result["assumptions"],
                      "supporting_evidence_ids": result["supporting_evidence_ids"],
                      "counter_evidence_ids": result["counter_evidence_ids"],
                      "review_trigger": result["review_trigger"],
                      "joint_event_id": payload["event_id"],
                      "joint_forecast_id": joint_id,
                      "joint_probabilities": result["probabilities"],
                      "raw_joint_probabilities": result.get("raw_probabilities"),
                      "review": review, "reviewed_at": reviewed_at,
                      "prompt_version": PROMPT_VERSION,
                      "model_version": CODEX_MODEL if provider == "codex" else DEEPSEEK_MODEL,
                      "provider": provider,
                      "valid_until": (datetime.fromisoformat(payload["as_of"]) +
                                      timedelta(hours=config.analysis_cooldown_hours)).isoformat(timespec="seconds")}
            prediction_id = save_prediction(conn, row["id"], own_rules, own_evidence, output)
            predictions.append({"market_id": row["id"], "prediction_id": prediction_id,
                                "rules_hash": own_rules, "evidence_hash": own_evidence,
                                "p_yes": None if output["abstain"] else output["p_yes"],
                                "abstain": output["abstain"]})
    for prediction in predictions:
        with database(config.database) as conn:
            staged = analysis_market_baseline(conn, prediction["market_id"],
                prediction["rules_hash"], prediction["evidence_hash"], payload["as_of"])
            saved_as_of = conn.execute("SELECT as_of FROM predictions WHERE id=?",
                                       (prediction["prediction_id"],)).fetchone()["as_of"]
            if staged and saved_as_of == payload["as_of"]:
                save_market_baseline(conn, prediction["prediction_id"], staged["captured_at"],
                                     staged["book_timestamp"], staged["yes_best_bid"],
                                     staged["yes_best_ask"])
                prediction["market_baseline"] = {"status": "captured", "phase": "pre_analysis",
                    "p_yes_mid": round((staged["yes_best_bid"] + staged["yes_best_ask"]) / 2, 6),
                    "spread": round(staged["yes_best_ask"] - staged["yes_best_bid"], 6)}
            else:
                prediction["market_baseline"] = {"status": "unavailable",
                    "reason": "No contemporaneous pre-analysis book"}
        prediction.pop("rules_hash")
        prediction.pop("evidence_hash")
    return {"event_id": payload["event_id"], "status": "analyzed",
            "review_approved": review["approved"], "review_reason": review["reason"],
            "joint_forecast_id": joint_id, "predictions": predictions}


def paper(config: Config, market_id: str) -> dict:
    with database(config.database) as conn:
        audited = conn.execute("SELECT event_id FROM markets WHERE id=?", (market_id,)).fetchone()
        prediction = conn.execute("""SELECT id FROM predictions WHERE market_id=?
            ORDER BY as_of DESC,id DESC LIMIT 1""", (market_id,)).fetchone()
        def decision(status: str, reason: str, order_id: int | None = None) -> None:
            if audited:
                conn.execute("""INSERT INTO paper_decisions
                    (market_id,event_id,prediction_id,checked_at,status,reason,order_id)
                    VALUES(?,?,?,?,?,?,?)""", (market_id, audited["event_id"],
                    prediction["id"] if prediction else None, now_iso(), status, reason, order_id))
        if calendar_due(conn):
            refresh_calendars(conn)
        expire_paper_intents(conn)
        risk = mark_account(conn, config)
        if risk["active_halts"]:
            decision("skipped", "Paper risk halted: " + ", ".join(risk["active_halts"]))
            return {"market_id": market_id, "status": "skipped",
                    "reason": "Paper risk halted: " + ", ".join(risk["active_halts"]),
                    "risk": risk}
        try:
            quote, reason = candidate(conn, market_id, config)
        except RuntimeError as exc:
            reason = f"Market data unavailable: {exc}"[:200]
            decision("skipped", reason)
            return {"market_id": market_id, "status": "skipped", "reason": reason}
        if quote is None:
            decision("skipped", reason)
            return {"market_id": market_id, "status": "skipped", "reason": reason}
        try:
            order_id = place(conn, quote, config)
        except (ValueError,RuntimeError) as exc:
            reason = f"Delayed paper execution skipped: {exc}"[:250]
            decision("skipped",reason)
            return {"market_id":market_id,"status":"skipped","reason":reason}
        filled = conn.execute("SELECT * FROM paper_orders WHERE id=?",(order_id,)).fetchone()
        decision("paper_filled", "Delayed snapshot FOK simulated order filled", order_id)
    return {"market_id": market_id, "status": "paper_filled", "order_id": order_id,
            "side": quote["side"], "shares": quote["shares"],
            "avg_price": round(filled["avg_price"], 6),
            "total_cost": round(filled["total_cost"], 6),
            "conservative_edge_per_share": round(filled["expected_edge"], 6)}


def cycle(config: Config) -> dict:
    provider = active_provider(config.data_dir)
    discovery_errors: list[str] = []
    markets = scan(config, errors=discovery_errors)
    with database(config.database) as conn:
        settlements = reconcile_short(conn, chainlink_path=config.data_dir / "chainlink_twap60.jsonl")
    sources = collect(config)
    with database(config.database) as conn:
        calendars = refresh_calendars(conn)
        expired_intents = expire_paper_intents(conn)
        pause_until = model_pause_until(conn, provider)
        calls_used = model_calls_today(conn, provider)
    analyses: list[dict] = []
    paper_checks: list[dict] = []
    provider_paused = bool(pause_until and pause_until > now_iso())
    daily_capped = calls_used >= config.model_call_limit(provider)
    model_unavailable = provider_paused or daily_capped
    model_status = {"provider": provider, "available": not model_unavailable,
                    "reason": ("provider_paused" if provider_paused else
                               "daily_call_cap" if daily_capped else None),
                    "pause_until": pause_until, "calls_today": calls_used}
    event_groups: dict[str, list[dict]] = {}
    for market in markets:
        event_groups.setdefault(market["event_id"], []).append(market)
    processed_events: set[str] = set()
    analyzed_categories: set[str] = set()
    ordered_markets = sorted(markets, key=lambda item: (
        {"crypto5m": 0, "daily": 1, "crypto": 2, "macro": 3}.get(item.get("category", "macro"), 4),
        item["end_at"], -item["volume_24h"]))
    for market in ordered_markets:
        if market.get("category") == "crypto5m" and not config.five_minute_auto_enabled():
            continue
        if market["event_id"] in processed_events:
            continue
        try:
            group = event_groups[market["event_id"]]
            if complete_fed_event(group):
                processed_events.add(market["event_id"])
                with database(config.database) as conn:
                    current = []
                    for sibling in group:
                        row = get_market(conn, sibling["id"])
                        if conn.execute("SELECT 1 FROM resolutions WHERE market_id=?",
                                        (sibling["id"],)).fetchone():
                            current = []
                            break
                        evidence = evidence_for_market(conn, row)
                        if not evidence:
                            current = []
                            break
                        payload = make_payload(dict(row), [dict(item) for item in evidence],
                                               now_iso(), provider)
                        rules_hash, evidence_hash = hashes(payload, config.analysis_cooldown_hours)
                        old = conn.execute("""SELECT id,abstain FROM predictions WHERE market_id=?
                            AND rules_hash=? AND evidence_hash=?""",
                            (sibling["id"], rules_hash, evidence_hash)).fetchone()
                        if old is None:
                            current = []
                            break
                        current.append((sibling["id"], bool(old["abstain"])))
                if len(current) == len(group):
                    for sibling_id, abstain in current:
                        if not abstain:
                            try:
                                paper_checks.append(paper(config, sibling_id))
                            except Exception as exc:
                                paper_checks.append({"market_id": sibling_id,
                                                     "status": "error", "message": str(exc)})
                    continue
                if model_unavailable or len(analyses) >= config.max_analyses_per_cycle:
                    continue
                analysis_result = analyze_event(config, group)
                for prediction in analysis_result.get("predictions", []):
                    if prediction["abstain"]:
                        continue
                    try:
                        prediction["paper"] = paper(config, prediction["market_id"])
                    except Exception as exc:
                        prediction["paper"] = {"status": "error", "message": str(exc)}
                analyses.append(analysis_result)
                continue
            if fed_outcome_slot(market["question"]):
                processed_events.add(market["event_id"])
                analyses.append({"event_id": market["event_id"], "status": "skipped",
                                 "reason": "Incomplete Fed outcome set"})
                continue
            with database(config.database) as conn:
                row = get_market(conn, market["id"])
                if conn.execute("SELECT 1 FROM resolutions WHERE market_id=?", (market["id"],)).fetchone():
                    continue
                evidence = evidence_for_market(conn, row)
                if row["category"] == "crypto5m" and not any(
                        item["source_id"] == "chainlink_btc_twap60" and
                        (observed := parse_utc(item["updated_at"])) is not None and
                        0 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 30
                        for item in evidence):
                    continue
                payload = make_payload(dict(row), [dict(e) for e in evidence], now_iso(), provider)
                rules_hash, evidence_hash = hashes(payload, config.analysis_cooldown_hours)
                old = conn.execute("SELECT id,abstain FROM predictions WHERE market_id=? AND rules_hash=? AND evidence_hash=?",
                                   (market["id"], rules_hash, evidence_hash)).fetchone()
            if not evidence:
                continue
            if old:
                # Prices move even when the evidence and forecast do not. Recheck fresh forecasts.
                if not old["abstain"]:
                    try:
                        paper_checks.append(paper(config, market["id"]))
                    except Exception as exc:
                        paper_checks.append({"market_id": market["id"], "status": "error", "message": str(exc)})
                continue
            if len(analyses) >= config.max_analyses_per_cycle:
                continue
            if model_unavailable:
                continue
            category = market.get("category", "macro")
            if category in analyzed_categories:
                continue
            if category == "crypto" and not hourly_window_ready(market, datetime.now(timezone.utc)):
                continue
            cutoff = datetime.fromisoformat(market["end_at"].replace("Z", "+00:00"))
            minimum_time = timedelta(seconds=90) if category == "crypto5m" else timedelta(minutes=12)
            if category in ("crypto", "crypto5m", "daily") and cutoff <= datetime.now(timezone.utc) + minimum_time:
                continue
            analysis_result = analyze(config, market["id"], once_per_window=category == "crypto5m")
            analyzed_categories.add(category)
            try:
                analysis_result["paper"] = ({"status": "paused"} if category == "crypto5m" and
                    not config.five_minute_auto_enabled() else paper(config, market["id"]))
            except Exception as exc:
                analysis_result["paper"] = {"status": "error", "message": str(exc)}
            analyses.append(analysis_result)
        except Exception as exc:
            if any(marker in str(exc) for marker in (
                    "provider paused", "call limit reached", "usage limit reached")):
                model_unavailable = True
                model_status["available"] = False
                model_status["reason"] = "provider_unavailable"
            analyses.append({"market_id": market["id"], "status": "error", "message": str(exc)})
    return {"time": now_iso(), "watchlist_markets": len(markets),
            "discovery": {"status": "partial" if discovery_errors else "succeeded",
                          "errors": discovery_errors},
            "sources": sources, "settlements": settlements, "calendars": calendars,
            "expired_paper_intents": expired_intents,
            "model_status": model_status,
            "analyses": analyses, "paper_checks": paper_checks}


def validate_btc5m(config: Config, rounds: int = 3, max_wait_minutes: int = 35) -> dict:
    """Manual live validation owns the five-minute worker lock for its full run."""
    with local_cycle_lock(config.data_dir / "btc5m.lock"):
        return _validate_btc5m_unlocked(config, rounds, max_wait_minutes)


def _validate_btc5m_unlocked(config: Config, rounds: int, max_wait_minutes: int) -> dict:
    """Run distinct live five-minute paper forecasts, then observe official final outcomes."""
    if not 1 <= rounds <= 10 or not 5 <= max_wait_minutes <= 120:
        raise ValueError("Invalid BTC 5m validation limits")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js 22+ is required for the Chainlink RTDS paper collector")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    collector_log = (config.data_dir / "chainlink-collector.err.log").open("a", encoding="utf-8")
    collector_env = node_collector_env()
    collector_env["POLYCAST_PARENT_PIPE"] = "1"
    collector = subprocess.Popen(
        [node, str(Path(__file__).with_name("chainlink_twap60.mjs")),
         str(config.data_dir / "chainlink_twap60.jsonl"), str(max_wait_minutes * 60)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=collector_log, env=collector_env,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
    collector_log.close()
    atexit.register(lambda: collector.terminate() if collector.poll() is None else None)
    with database(config.database) as conn:
        attempted: set[str] = {row["market_id"] for row in conn.execute("""SELECT DISTINCT p.market_id
            FROM predictions p JOIN markets m ON m.id=p.market_id
            WHERE m.category='crypto5m' AND m.end_at>?""", (now_iso(),))}
    results: list[dict] = []
    deadline = time.monotonic() + max_wait_minutes * 60
    while len(results) < rounds and time.monotonic() < deadline:
        now = datetime.now(timezone.utc)
        markets = discover_btc_5m(now)
        eligible = [market for market in markets if market["id"] not in attempted and
                    datetime.fromtimestamp(int(market["source_ref"].split(":")[1]), timezone.utc)
                    + timedelta(seconds=80) <= now <
                    datetime.fromisoformat(market["end_at"]) - timedelta(seconds=100)]
        if not eligible:
            time.sleep(10)
            continue
        market = eligible[0]
        item: dict = {"market_id": market["id"], "event_id": market["event_id"],
                      "window_end": market["end_at"]}
        try:
            with database(config.database) as conn:
                if conn.execute("SELECT 1 FROM predictions WHERE market_id=? LIMIT 1",
                                (market["id"],)).fetchone():
                    attempted.add(market["id"])
                    continue
                if conn.execute("""SELECT 1 FROM model_runs WHERE market_id=?
                    AND status='running' LIMIT 1""", (market["id"],)).fetchone():
                    time.sleep(10)
                    continue
                upsert_markets(conn, [market], reset_watchlist=False)
                item["evidence"] = collect_short(
                    conn, [market], config.data_dir / "chainlink_twap60.jsonl")
                row = get_market(conn, market["id"])
                has_twap = any(e["source_id"] == "chainlink_btc_twap60"
                               for e in evidence_for_market(conn, row))
            if not has_twap:
                time.sleep(10)
                continue
            forecast = analyze(config, market["id"])
            item["analysis"] = forecast
            if forecast.get("status") in ("analyzed", "unchanged"):
                item["paper"] = paper(config, market["id"])
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"[:250]
        attempted.add(market["id"])
        results.append(item)
        _json({"btc5m_round": len(results), **item})
        sys.stdout.flush()
    pending = {item["market_id"] for item in results if "analysis" in item}
    settlements = []
    while pending and time.monotonic() < deadline:
        with database(config.database) as conn:
            outcome = reconcile_short(conn, categories=("crypto5m",),
                                      chainlink_path=config.data_dir / "chainlink_twap60.jsonl")
            settled = {row["market_id"] for row in conn.execute(
                "SELECT market_id FROM resolutions WHERE market_id IN (" +
                ",".join("?" for _ in pending) + ")", tuple(pending))}
        for item in outcome["settled"]:
            settlements.append(item)
            _json({"btc5m_settlement": item})
            sys.stdout.flush()
        pending -= settled
        if pending:
            time.sleep(15)
    return {"attempted_events": len(results), "settled_events": len(results) - len(pending),
            "pending_market_ids": sorted(pending), "results": results,
            "settlements": settlements}


def status(config: Config) -> dict:
    provider = active_provider(config.data_dir)
    with database(config.database) as conn:
        totals = balance(conn, config.initial_cash)
        counts = {
            "watchlist_markets": conn.execute("SELECT COUNT(*) FROM markets WHERE in_watchlist=1").fetchone()[0],
            "evidence_versions": conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "predictions": conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0],
            "paper_orders": conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0],
            "reserved_paper_intents": conn.execute("""SELECT COUNT(*) FROM trade_intents
                WHERE status IN ('reserved','unknown')""").fetchone()[0],
            "paper_intents": conn.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0],
            "resolved_markets": conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0],
            "model_provider": provider,
            "model_version": CODEX_MODEL if provider == "codex" else DEEPSEEK_MODEL,
            "model_calls_today": model_calls_today(conn, provider),
            "model_calls_daily_limit": config.model_call_limit(provider),
            "model_pause_until": model_pause_until(conn, provider),
            "pending_analysis_drafts": conn.execute("""SELECT COUNT(*) FROM analysis_drafts d
                WHERE NOT EXISTS (SELECT 1 FROM predictions p WHERE p.market_id=d.market_id
                AND p.rules_hash=d.rules_hash AND p.evidence_hash=d.evidence_hash)""").fetchone()[0],
            "enabled_news_sources": conn.execute("""SELECT COUNT(*) FROM source_registry
                WHERE kind='news' AND enabled=1""").fetchone()[0],
            "withdrawn_evidence_urls": conn.execute(
                "SELECT COUNT(*) FROM evidence_withdrawals").fetchone()[0],
            "active_risk_halts": active_halts(conn),
            "calendar_fetches": [dict(row) for row in conn.execute(
                "SELECT source,source_url,fetched_at,content_hash FROM calendar_fetches ORDER BY source")],
        }
        risk_row = conn.execute("""SELECT captured_at,equity,day_start_equity,
            high_water_equity FROM risk_snapshots ORDER BY id DESC LIMIT 1""").fetchone()
        recent = [dict(x) for x in conn.execute("""SELECT p.id,p.market_id,m.question,p.as_of,p.probability,p.abstain,p.reasoning
            FROM predictions p JOIN markets m ON p.market_id=m.id ORDER BY p.id DESC LIMIT 10""")]
        orders = [dict(x) for x in conn.execute("""SELECT o.id,o.market_id,m.question,o.side,o.shares,o.avg_price,
            o.total_cost,o.expected_edge,o.status,o.settlement,o.created_at
            FROM paper_orders o JOIN markets m ON o.market_id=m.id ORDER BY o.id DESC LIMIT 20""")]
        intents = [dict(x) for x in conn.execute("""SELECT i.id,i.prediction_id,i.market_id,
            m.question,i.side,i.shares,i.limit_price,i.max_total_cost,i.time_in_force,
            i.status,i.created_at,i.updated_at,i.expires_at,i.failure_reason
            FROM trade_intents i JOIN markets m ON m.id=i.market_id
            ORDER BY i.id DESC LIMIT 20""")]
        runs = [dict(x) for x in conn.execute("""SELECT id,started_at,finished_at,status,
            model_calls,watchlist_markets,evidence_fetched,new_evidence_versions,
            analysis_attempts,paper_checks,paper_fills,error_count,errors_json
            FROM cycle_runs ORDER BY id DESC LIMIT 10""")]
    return {"mode": "paper-only", **counts, "account": totals,
            "latest_risk_snapshot": dict(risk_row) if risk_row else None,
            "recent_predictions": recent, "paper_orders_detail": orders,
            "recent_trade_intents": intents, "recent_cycle_runs": runs}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="polycast", description="Multi-sector prediction research and paper trading")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="refresh hourly crypto, daily MLB, and Fed watchlist")
    sub.add_parser("collect", help="collect official Federal Reserve and BLS RSS evidence")
    sub.add_parser("sources", help="show source permissions and recorded withdrawals")
    evidence_cmd = sub.add_parser("evidence", help="audit active article versions and public release times")
    evidence_cmd.add_argument("--limit", type=int, default=20)
    sub.add_parser("calendar", help="refresh official BLS and FOMC release schedules")
    sub.add_parser("markets", help="show local watchlist")
    analysis_cmd = sub.add_parser("analyze", help="run one prediction with the selected model provider")
    analysis_cmd.add_argument("market_id")
    paper_cmd = sub.add_parser("paper", help="evaluate and simulate one immediate buy")
    paper_cmd.add_argument("market_id")
    sub.add_parser("cycle", help="scan, collect, and analyze a small number of changed markets")
    sub.add_parser("reconcile", help="verify and settle closed short-cycle paper markets")
    btc_cmd = sub.add_parser("validate-btc5m", help="paper-validate consecutive BTC five-minute markets")
    btc_cmd.add_argument("--rounds", type=int, default=3)
    btc_cmd.add_argument("--max-wait-minutes", type=int, default=35)
    anchor_cmd = sub.add_parser("audit-btc5m-anchors", help="compare locally discovered BTC 5m Gamma outcomes with captured TWAP boundary proxies")
    anchor_cmd.add_argument("--limit", type=int, default=50)
    watch_cmd = sub.add_parser("watch", help="repeat paper-trading cycles; Ctrl+C to stop")
    watch_cmd.add_argument("--interval-minutes", type=int, default=30)
    watch_cmd.add_argument("--max-cycles", type=int, default=0, help="0 means run until stopped")
    sub.add_parser("status", help="show evidence, predictions, paper orders, and balance")
    provider_cmd = sub.add_parser("provider", help="show or select Codex / DeepSeek")
    provider_cmd.add_argument("name", nargs="?", choices=("codex", "deepseek"))
    provider_cmd.add_argument("--limit", type=int, help="daily call limit for provider")
    settings_cmd = sub.add_parser("settings", help="inspect or update runtime settings")
    settings_cmd.add_argument("--deepseek-limit", type=int, help="set DeepSeek daily call limit")
    settings_cmd.add_argument("--codex-limit", type=int, help="set Codex daily call limit")
    key_cmd = sub.add_parser("deepseek-key", help="set or inspect the encrypted local API key")
    key_cmd.add_argument("action", choices=("set", "status"))
    resume_cmd = sub.add_parser("risk-resume", help="clear paper-only risk stops after reconciliation")
    resume_cmd.add_argument("--reason", required=True,
                            help="specific cause, reconciliation, and recovery rationale")
    settle_cmd = sub.add_parser("settle", help="record a VERIFIED final market outcome for paper P&L")
    settle_cmd.add_argument("market_id")
    settle_cmd.add_argument("winning_side", choices=["YES", "NO"])
    settle_cmd.add_argument("--source-url", required=True, help="verified final result or resolution source")
    settle_cmd.add_argument("--resolved-at", required=True, help="public result time with UTC offset")
    withdraw_cmd = sub.add_parser("withdraw-evidence", help="exclude a sourced withdrawn article from new analysis")
    withdraw_cmd.add_argument("url", help="archived article URL")
    withdraw_cmd.add_argument("--source-url", required=True, help="HTTPS correction or withdrawal notice")
    withdraw_cmd.add_argument("--reason", required=True, help="specific correction or withdrawal reason")
    sub.add_parser("report", help="event-group evaluation from verified outcomes")
    serve_cmd = sub.add_parser("serve", help="local paper dashboard and task controls")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    config = Config()
    config.validate()
    try:
        if args.command == "scan":
            markets = scan(config)
            _json({"count": len(markets), "markets": [{"id": x["id"], "event": x["event_title"],
                "question": x["question"], "volume_24h": x["volume_24h"]} for x in markets]})
        elif args.command == "collect":
            _json(collect(config))
        elif args.command == "sources":
            with database(config.database) as conn:
                _json({"sources": [dict(row) for row in conn.execute("""SELECT source_id,source,kind,
                    feed_url,license_scope,license_reference,license_checked_at,enabled,
                    first_seen_at,last_seen_at FROM source_registry ORDER BY kind,source_id""")],
                    "withdrawals": [dict(row) for row in conn.execute("""SELECT url,source_url,
                    reason,recorded_at FROM evidence_withdrawals ORDER BY recorded_at DESC""")]})
        elif args.command == "evidence":
            if not 1 <= args.limit <= 100:
                raise ValueError("Evidence limit must be between 1 and 100")
            with database(config.database) as conn:
                _json([dict(row) for row in conn.execute("""SELECT e.id,e.source,e.source_id,
                    e.title,e.url,e.published_at,e.feed_published_at,e.updated_at,
                    e.first_seen_at,e.fetched_at,e.last_seen_at,e.origin_story_id,
                    c.activation_seq,s.enabled,w.recorded_at withdrawn_at
                    FROM evidence_current c JOIN evidence e ON e.id=c.evidence_id
                    LEFT JOIN source_registry s ON s.source_id=e.source_id
                    LEFT JOIN evidence_withdrawals w ON w.url=e.url
                    ORDER BY e.published_at DESC,e.id DESC LIMIT ?""", (args.limit,))])
        elif args.command == "calendar":
            with database(config.database) as conn:
                _json(refresh_calendars(conn, force=True))
        elif args.command == "markets":
            with database(config.database) as conn:
                _json([dict(row) for row in conn.execute("""SELECT id,event_id,event_title,question,end_at,
                    liquidity,volume_24h FROM markets WHERE in_watchlist=1
                    ORDER BY volume_24h DESC LIMIT 30""")])
        elif args.command == "analyze":
            _json(analyze(config, args.market_id))
        elif args.command == "paper":
            _json(paper(config, args.market_id))
        elif args.command == "cycle":
            _json(run_recorded(config, lambda: cycle(config)))
        elif args.command == "reconcile":
            with database(config.database) as conn:
                _json(reconcile_short(conn, chainlink_path=config.data_dir / "chainlink_twap60.jsonl"))
        elif args.command == "validate-btc5m":
            _json(validate_btc5m(config, args.rounds, args.max_wait_minutes))
        elif args.command == "audit-btc5m-anchors":
            with database(config.database) as conn:
                _json(audit_btc5m_anchors(conn, config.data_dir / "chainlink_twap60.jsonl",
                                          limit=args.limit))
        elif args.command == "watch":
            if not 1 <= args.interval_minutes <= 1440 or args.max_cycles < 0:
                raise ValueError("Invalid watch interval or cycle count")
            completed = 0
            try:
                while args.max_cycles == 0 or completed < args.max_cycles:
                    try:
                        outcome = run_recorded(config, lambda: cycle(config))
                        _json({key: outcome[key] for key in ("run_id", "status", "summary")})
                    except Exception as exc:
                        _json({"status": "failed", "message": f"{type(exc).__name__}: {exc}"[:250]})
                    completed += 1
                    sys.stdout.flush()
                    if args.max_cycles == 0 or completed < args.max_cycles:
                        time.sleep(args.interval_minutes * 60)
            except KeyboardInterrupt:
                print("Stopped watch loop")
        elif args.command == "status":
            _json(status(config))
        elif args.command == "provider":
            if args.name:
                select_provider(config.data_dir, args.name)
            target = args.name or active_provider(config.data_dir)
            if getattr(args, "limit", None) is not None:
                if args.limit < 1:
                    raise ValueError("Daily call limit must be at least 1")
                key = "max_deepseek_calls_per_utc_day" if target == "deepseek" else "max_model_calls_per_utc_day"
                save_runtime_settings(config.data_dir, {key: args.limit})
            active = active_provider(config.data_dir)
            _json({"provider": active,
                   "model_version": CODEX_MODEL if active == "codex" else DEEPSEEK_MODEL,
                   "deepseek_configured": has_deepseek_key(config.data_dir),
                   "daily_limit": config.model_call_limit(active)})
        elif args.command == "settings":
            updates = {}
            if getattr(args, "deepseek_limit", None) is not None:
                if args.deepseek_limit < 1:
                    raise ValueError("DeepSeek call limit must be at least 1")
                updates["max_deepseek_calls_per_utc_day"] = args.deepseek_limit
            if getattr(args, "codex_limit", None) is not None:
                if args.codex_limit < 1:
                    raise ValueError("Codex call limit must be at least 1")
                updates["max_model_calls_per_utc_day"] = args.codex_limit
            if updates:
                save_runtime_settings(config.data_dir, updates)
            active = active_provider(config.data_dir)
            _json({"provider": active,
                   "deepseek_daily_limit": config.model_call_limit("deepseek"),
                   "codex_daily_limit": config.model_call_limit("codex"),
                   "settings": load_runtime_settings(config.data_dir)})
        elif args.command == "deepseek-key":
            if args.action == "set":
                secret = getpass.getpass("DeepSeek API key: ") if sys.stdin.isatty() else sys.stdin.readline().strip()
                save_deepseek_key(config.data_dir, secret)
            _json({"deepseek_configured": has_deepseek_key(config.data_dir)})
        elif args.command == "risk-resume":
            with database(config.database) as conn:
                _json(resume_after_reconciliation(conn, config, args.reason))
        elif args.command == "settle":
            with database(config.database) as conn:
                updated = settle(conn, args.market_id, args.winning_side, args.source_url, args.resolved_at)
            _json({"settled_paper_orders": updated, "market_id": args.market_id,
                   "winning_side": args.winning_side, "source_url": args.source_url,
                   "resolved_at": args.resolved_at})
        elif args.command == "withdraw-evidence":
            with database(config.database) as conn:
                created = withdraw_evidence(conn, args.url, args.source_url, args.reason)
            _json({"url": args.url, "withdrawn": created, "source_url": args.source_url})
        elif args.command == "report":
            from .evaluation import report
            with database(config.database) as conn:
                _json(report(conn))
        elif args.command == "serve":
            from .dashboard import serve
            serve(config, args.host, args.port)
        return 0
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

