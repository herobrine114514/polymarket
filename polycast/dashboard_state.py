"""Read-only, serializable state for the local paper-trading dashboard."""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import Config, load_runtime_settings
from .analysis import CODEX_MODEL
from .analysis import BTC5M_PROMPT_VERSION
from .evaluation import report
from .market import complete_fed_event, fed_outcome_slot, live_market, parse_utc
from .paper import _live_matches, _prediction_current, _quote
from .provider import DEEPSEEK_MODEL, active_provider, has_deepseek_key
from .research_shadow import shadow_status
from .risk import active_halts
from .scheduler_control import TASK_NAMES
from .store import balance, get_market, model_calls_today, model_pause_until, now_iso, read_database


_task_cache: dict[str, tuple[float, dict]] = {}
_task_lock = threading.Lock()


def invalidate_task_cache() -> None:
    with _task_lock:
        _task_cache.clear()


def scheduled_task(target: str = "cycle") -> dict:
    """Inspect the known Windows task without changing it."""
    if target not in TASK_NAMES:
        raise ValueError("Unknown Polycast task")
    if os.name != "nt":
        return {"available": False, "reason": "Windows Task Scheduler is unavailable"}
    with _task_lock:
        cached = _task_cache.get(target)
        if cached and time.monotonic() - cached[0] < 20:
            return cached[1]
        timing = ("$end=$null;$interval=$null;"
                  "$repeat=@($t.Triggers | Where-Object {$_.Repetition.Interval -eq 'PT1M'});"
                  "if($repeat.Count){$interval=1};$next=$null;"
                  "if($i.NextRunTime){$next=$i.NextRunTime.ToString('o')};" if target == "realtime" else
                  "$end=([datetimeoffset]::Parse($t.Triggers[0].StartBoundary)+"
                  "[System.Xml.XmlConvert]::ToTimeSpan($t.Triggers[0].Repetition.Duration)).ToString('o');"
                  "$interval=[int][System.Xml.XmlConvert]::ToTimeSpan($t.Triggers[0].Repetition.Interval).TotalMinutes;"
                  "$next=$i.NextRunTime.ToString('o');")
        script = (
            f"$t=Get-ScheduledTask -TaskName '{TASK_NAMES[target]}' -ErrorAction Stop;"
            "$i=$t | Get-ScheduledTaskInfo -ErrorAction Stop;"
            + timing +
            "$background=[string]$t.Actions[0].Execute -match 'pythonw.exe$';"
            "[pscustomobject]@{available=$true;state=[string]$t.State;"
            f"task_name='{TASK_NAMES[target]}';persistent=${str(target == 'realtime').lower()};"
            "last_run=$i.LastRunTime.ToString('o');"
            "next_run=$next;repetition_end=$end;interval_minutes=$interval;"
            "background=[bool]$background;"
            "last_result=[long]$i.LastTaskResult;"
            "missed_runs=[int]$i.NumberOfMissedRuns} | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=8, creationflags=0x08000000,
            )
            if result.returncode:
                value = {"available": False, "reason": (result.stderr.strip() or "Task not found")[:160]}
            else:
                value = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            value = {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:160]}
        _task_cache[target] = (time.monotonic(), value)
        return value


def validation_progress(conn) -> dict:
    """One round means one independent event with reviewed forecast, paper decision and full outcome."""
    evaluation = report(conn)
    complete = {group["event_ids"][0] for group in evaluation["groups"]
                if group["complete_for_validation"] and len(group["event_ids"]) == 1}
    rows = conn.execute("""SELECT event_id,MAX(event_title) event_title,MAX(end_at) end_at,
        COUNT(*) market_count,SUM(in_watchlist) watchlist_count
        FROM markets GROUP BY event_id ORDER BY end_at,event_id""").fetchall()
    events = []
    for row in rows:
        event_id = row["event_id"]
        if not row["watchlist_count"] and event_id not in complete:
            continue
        resolution = conn.execute("""SELECT MIN(r.resolved_at) first_outcome,
            COUNT(*) resolved_count FROM resolutions r JOIN markets m ON m.id=r.market_id
            WHERE m.event_id=?""", (event_id,)).fetchone()
        first_outcome = parse_utc(resolution["first_outcome"]) if resolution["first_outcome"] else None
        event_markets = [dict(item) for item in conn.execute(
            "SELECT id,question,category FROM markets WHERE event_id=?", (event_id,))]
        five_minute = any(item["category"] == "crypto5m" for item in event_markets)
        forecasts = conn.execute("""SELECT p.id,p.market_id,p.as_of,p.abstain,p.probability,p.model_output_json
            FROM predictions p JOIN markets m ON m.id=p.market_id
            WHERE m.event_id=? ORDER BY p.id""", (event_id,)).fetchall()
        eligible: dict[int, datetime] = {}
        latest_by_market: dict[str, tuple[str, int, float, dict]] = {}
        for prediction in forecasts:
            try:
                output = json.loads(prediction["model_output_json"])
                review_at = parse_utc(output.get("reviewed_at"))
                forecast_at = parse_utc(prediction["as_of"])
                if (not prediction["abstain"] and prediction["probability"] is not None and
                        output.get("review", {}).get("approved") is True and
                        review_at and forecast_at and forecast_at <= review_at and
                        (first_outcome is None or review_at <= first_outcome)):
                    eligible[int(prediction["id"])] = review_at
                    prior = latest_by_market.get(prediction["market_id"])
                    key = (prediction["as_of"], int(prediction["id"]))
                    if prior is None or key > prior[:2]:
                        latest_by_market[prediction["market_id"]] = (
                            *key, float(prediction["probability"]), output)
            except (ValueError, TypeError, AttributeError):
                continue
        prediction_ready = (bool(event_markets) and
                            set(latest_by_market) == {item["id"] for item in event_markets})
        if any(fed_outcome_slot(item["question"]) for item in event_markets):
            prediction_ready = prediction_ready and complete_fed_event(event_markets)
            if prediction_ready:
                outputs = [item[3] for item in latest_by_market.values()]
                joint_ids = {output.get("joint_forecast_id") for output in outputs}
                probability_map = {market_id: item[2] for market_id, item in latest_by_market.items()}
                prediction_ready = (len(joint_ids) == 1 and None not in joint_ids and
                                    all(output.get("joint_event_id") == event_id and
                                        output.get("joint_probabilities") == probability_map
                                        for output in outputs) and
                                    abs(sum(probability_map.values()) - 1) <= 1e-6)
        decisions = conn.execute("""SELECT d.prediction_id,d.checked_at,d.status,d.reason,
            o.status order_status FROM paper_decisions d
            LEFT JOIN paper_orders o ON o.id=d.order_id
            WHERE d.event_id=? ORDER BY d.id""", (event_id,)).fetchall()
        paper_decision = False
        decision_at = None
        for decision in decisions:
            checked = parse_utc(decision["checked_at"])
            valid_decision = (decision["status"] == "skipped" and
                              decision["reason"].startswith("Best conservative net edge ")) or (
                              decision["status"] == "paper_filled" and
                              decision["order_status"] in ("open", "settled"))
            if (valid_decision and decision["prediction_id"] in eligible and checked and
                    checked >= eligible[decision["prediction_id"]] and
                    (not five_minute or (checked - eligible[decision["prediction_id"]]).total_seconds() <= 30) and
                    (first_outcome is None or checked < first_outcome)):
                paper_decision = True
                decision_at = decision["checked_at"]
                break
        # Orders made before the audit table existed remain valid paper decisions.
        if not paper_decision and eligible:
            for order in conn.execute("""SELECT prediction_id,created_at FROM paper_orders
                WHERE event_id=? AND status IN ('open','settled') ORDER BY id""", (event_id,)):
                placed = parse_utc(order["created_at"])
                if (order["prediction_id"] in eligible and placed and
                        placed >= eligible[order["prediction_id"]] and
                        (not five_minute or (placed - eligible[order["prediction_id"]]).total_seconds() <= 30) and
                        (first_outcome is None or placed < first_outcome)):
                    paper_decision, decision_at = True, order["created_at"]
                    break
        events.append({"event_id": event_id, "title": row["event_title"],
                       "end_at": row["end_at"], "market_count": row["market_count"],
                       "prediction": bool(prediction_ready), "paper_decision": paper_decision,
                       "decision_at": decision_at,
                       "settlement": event_id in complete,
                       "resolved_count": resolution["resolved_count"],
                       "complete": bool(prediction_ready and paper_decision and event_id in complete)})
    done = sum(item["complete"] for item in events)
    return {"target": 3, "completed": done, "achieved": done >= 3,
            "events": events, "evaluation": evaluation}


def live_quote(config: Config, market_id: str) -> dict:
    """Fetch one market's current executable asks for display; never place an order."""
    if not market_id.isdigit():
        raise ValueError("Invalid market ID")
    with read_database(config.database) as conn:
        market = get_market(conn, market_id)
        prediction = conn.execute("""SELECT *
            FROM predictions WHERE market_id=? ORDER BY as_of DESC,id DESC LIMIT 1""",
            (market_id,)).fetchone()
        forecast_current = bool(prediction and _prediction_current(conn, market, prediction, config))
    result = {"market_id": market_id, "captured_at": now_iso(),
              "shares": max(5.0, float(market["min_order_size"])),
              "fee_rate": float(market["fee_rate"]), "quotes": [],
              "forecast_eligible": False, "reason": None}
    try:
        if not _live_matches(market, live_market(market_id)):
            result["reason"] = "市场规则、费用、代币映射或交易状态变化；请重新扫描"
            return result
        if prediction and not prediction["abstain"] and prediction["probability"] is not None:
            output = json.loads(prediction["model_output_json"] or "{}")
            valid_until = parse_utc(output.get("valid_until"))
            result["forecast_eligible"] = bool(
                output.get("review", {}).get("approved") is True and
                parse_utc(output.get("reviewed_at")) and valid_until and
                valid_until > datetime.now(timezone.utc) and forecast_current)
            if not result["forecast_eligible"]:
                result["reason"] = "预测未通过审查、证据变化或短周期时效已结束"
        else:
            result["reason"] = "没有可用的经审查预测"
        for side in ("YES", "NO"):
            quote = _quote(market, side, result["shares"], config)
            if quote is None:
                continue
            item = {"side": side, "avg_ask": quote["avg_ask"],
                    "worst_ask": quote["worst_ask"],
                    "fee_per_share": quote["fee_per_share"],
                    "book_timestamp": quote["book_timestamp"], "edge": None}
            if result["forecast_eligible"]:
                probability = float(prediction["probability"])
                conservative_p = (probability if side == "YES" else 1 - probability)
                item["edge"] = round(conservative_p - config.probability_haircut -
                                     quote["avg_ask"] - quote["fee_per_share"], 6)
            result["quotes"].append(item)
        if not result["quotes"]:
            result["reason"] = "没有足够深度且新鲜的订单簿"
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        result["reason"] = f"盘口不可用：{type(exc).__name__}: {exc}"[:180]
    return result


def forecast_basis_status(conn, config: Config) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = conn.execute('''SELECT prediction_id,checked_at,audit_json FROM forecast_book_checks
        WHERE julianday(checked_at)>=julianday(?) ORDER BY id DESC LIMIT 5001''', (cutoff,)).fetchall()
    truncated = len(rows) > 5000
    rows = rows[:5000]
    counts = dict.fromkeys(('valid', 'drifted', 'expired', 'invalid', 'unknown'), 0)
    latest_change = None
    for index, record in enumerate(rows):
        try:
            audit = json.loads(record['audit_json'])
            status = audit.get('status') if isinstance(audit, dict) else None
            counts[status if status in counts else 'unknown'] += 1
            change = audit.get('midpoint_change') if isinstance(audit, dict) else None
            if index == 0 and type(change) in (float, int) and math.isfinite(change) and abs(change) <= 1:
                latest_change = change
        except (ValueError, TypeError):
            counts['unknown'] += 1
    return {'scope': 'candidate_checks_last_24h', 'checks': len(rows), 'truncated': truncated,
            'unique_predictions': len({r['prediction_id'] for r in rows}), 'counts': counts,
            'last_checked_at': rows[0]['checked_at'] if rows else None,
            'latest_midpoint_change': latest_change,
            'max_mid_change': config.btc5m_book_max_drift,
            'max_forecast_age_seconds': config.btc5m_basis_max_age_seconds,
            'prompt_version': BTC5M_PROMPT_VERSION,
            'gain_validated': False}


def snapshot(config: Config) -> dict:
    provider = active_provider(config.data_dir)
    with read_database(config.database) as conn:
        account = balance(conn, config.initial_cash)
        basis_status = forecast_basis_status(conn, config)
        validation = validation_progress(conn)
        halts = active_halts(conn)
        risk = conn.execute("""SELECT captured_at,equity,day_start_equity,high_water_equity
            FROM risk_snapshots ORDER BY id DESC LIMIT 1""").fetchone()
        markets = [dict(row) for row in conn.execute("""SELECT m.id,m.event_id,m.event_title,
            m.event_slug,m.question,m.end_at,m.volume_24h,m.category,m.yes_label,m.no_label,
            p.id prediction_id,p.probability,
            p.abstain,p.as_of,p.reasoning,p.model_output_json,b.captured_at baseline_at,
            b.yes_best_bid,b.yes_best_ask,b.p_yes_mid,
            (SELECT COUNT(*) FROM paper_orders o WHERE o.market_id=m.id AND o.status='open') open_orders
            FROM markets m LEFT JOIN predictions p ON p.id=(SELECT id FROM predictions
                WHERE market_id=m.id ORDER BY id DESC LIMIT 1)
            LEFT JOIN market_baselines b ON b.prediction_id=p.id
            WHERE m.in_watchlist=1 AND (m.category='macro' OR m.end_at>?)
            ORDER BY CASE m.category WHEN 'crypto5m' THEN 0 WHEN 'crypto' THEN 1 WHEN 'daily' THEN 2 ELSE 3 END,
            m.end_at,m.volume_24h DESC LIMIT 40""", (now_iso(),))]
        predictions = [dict(row) for row in conn.execute("""SELECT p.id,p.market_id,p.as_of,
            p.probability,p.abstain,p.reasoning,p.assumptions_json,p.supports_json,
            p.counters_json,p.model_output_json,m.question,m.event_title,m.yes_label
            FROM predictions p JOIN markets m ON m.id=p.market_id ORDER BY p.id DESC LIMIT 20""")]
        evidence = [dict(row) for row in conn.execute("""SELECT e.id,e.source,e.title,e.url,
            e.published_at FROM evidence e JOIN evidence_current c ON c.url=e.url
            AND c.evidence_id=e.id JOIN source_registry s ON s.source_id=e.source_id
            WHERE s.enabled=1 AND s.license_scope=e.license_scope
            AND (s.kind='official' OR s.feed_url=e.feed_url)
            AND NOT EXISTS (SELECT 1 FROM evidence_withdrawals w WHERE w.url=e.url)
            ORDER BY e.published_at DESC LIMIT 30""")]
        citation_ids: set[int] = set()
        for prediction in predictions:
            for key in ("supports_json", "counters_json"):
                citation_ids.update(json.loads(prediction[key] or "[]"))
        cited_evidence = []
        if citation_ids:
            placeholders = ",".join("?" for _ in citation_ids)
            cited_evidence = [dict(row) for row in conn.execute(
                f"SELECT id,title,url FROM evidence WHERE id IN ({placeholders})",
                tuple(citation_ids))]
        orders = [dict(row) for row in conn.execute("""SELECT o.id,o.market_id,m.question,
            o.side,o.shares,o.avg_price,o.total_cost,o.expected_edge,o.status,o.settlement,o.created_at,
            m.end_at,s.status settlement_check_status,s.reason settlement_check_reason,
            s.checked_at settlement_checked_at,s.official_side official_winning_side
            FROM paper_orders o JOIN markets m ON m.id=o.market_id
            LEFT JOIN settlement_checks s ON s.market_id=o.market_id ORDER BY o.id DESC LIMIT 30""")]
        decisions = [dict(row) for row in conn.execute("""SELECT d.id,d.market_id,m.question,
            d.prediction_id,d.checked_at,d.status,d.reason,d.order_id
            FROM paper_decisions d JOIN markets m ON m.id=d.market_id ORDER BY d.id DESC LIMIT 30""")]
        intents = [dict(row) for row in conn.execute("""SELECT i.id,i.market_id,m.question,
            i.side,i.max_total_cost,i.status,i.created_at,i.failure_reason
            FROM trade_intents i JOIN markets m ON m.id=i.market_id ORDER BY i.id DESC LIMIT 20""")]
        runs = [dict(row) for row in conn.execute("""SELECT id,started_at,finished_at,status,
            watchlist_markets,new_evidence_versions,analysis_attempts,paper_checks,
            paper_fills,error_count,errors_json FROM cycle_runs ORDER BY id DESC LIMIT 20""")]
        calendar = [dict(row) for row in conn.execute("""SELECT source,name,starts_at,ends_at,
            source_url FROM calendar_windows WHERE ends_at>=? ORDER BY starts_at LIMIT 12""",
            (now_iso(),))]
        exposure = [dict(row) for row in conn.execute("""SELECT market_id,risk_group,cost FROM (
            SELECT market_id,risk_group,total_cost cost FROM paper_orders WHERE status='open'
            UNION ALL SELECT market_id,risk_group,max_total_cost cost FROM trade_intents
            WHERE status IN ('reserved','unknown'))""")]
        stats = {"evidence": conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
                 "news_sources": conn.execute("""SELECT COUNT(*) FROM source_registry
                     WHERE kind='news' AND enabled=1""").fetchone()[0],
                 "predictions": conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0],
                 "withdrawn": conn.execute("SELECT COUNT(*) FROM evidence_withdrawals").fetchone()[0]}
        model = {"provider": provider,
                 "model_version": CODEX_MODEL if provider == "codex" else DEEPSEEK_MODEL,
                 "deepseek_configured": has_deepseek_key(config.data_dir),
                 "calls_today": model_calls_today(conn, provider),
                 "daily_limit": config.model_call_limit(provider),
                 "pause_until": model_pause_until(conn, provider)}
    risk_dict = dict(risk) if risk else None
    captured = parse_utc(risk_dict["captured_at"]) if risk_dict else None
    risk_fresh = bool(captured and 0 <=
                      (datetime.now(timezone.utc) - captured).total_seconds() <=
                      config.quote_max_age_seconds)
    per_market: dict[str, float] = {}
    per_group: dict[str, float] = {}
    for item in exposure:
        per_market[item["market_id"]] = per_market.get(item["market_id"], 0) + item["cost"]
        per_group[item["risk_group"]] = per_group.get(item["risk_group"], 0) + item["cost"]
    for market in markets:
        output = json.loads(market.pop("model_output_json") or "{}")
        market["review_approved"] = output.get("review", {}).get("approved") is True
        market["exposure"] = per_market.get(market["id"], 0)
    evidence_map = {item["id"]: item for item in evidence}
    evidence_map.update({item["id"]: item for item in cited_evidence})
    for prediction in predictions:
        output = json.loads(prediction.pop("model_output_json") or "{}")
        prediction["review"] = output.get("review", {})
        prediction["model_version"] = output.get("model_version")
        prediction["reviewed_at"] = output.get("reviewed_at")
        prediction["review_trigger"] = output.get("review_trigger")
        for key in ("supports", "counters"):
            ids = json.loads(prediction.pop(key + "_json") or "[]")
            prediction[key] = [evidence_map.get(i, {"id": i, "title": f"证据 #{i} 不在最近列表", "url": None}) for i in ids]
        prediction["assumptions"] = json.loads(prediction.pop("assumptions_json") or "[]")
    total = sum(item["cost"] for item in exposure)
    return {"generated_at": now_iso(), "mode": "paper-only", "account": account,
            "model_costs": validation["evaluation"]["model_costs"],
            "calibration_shadow": shadow_status(config.data_dir),
            "forecast_basis": basis_status,
            "stats": stats, "markets": markets, "predictions": predictions,
            "evidence": evidence, "orders": orders, "decisions": decisions,
            "intents": intents, "runs": runs, "calendar": calendar,
            "validation": validation, "scheduler": scheduled_task(), "model": model,
            "realtime_scheduler": scheduled_task("realtime"),
            "settings": load_runtime_settings(config.data_dir),
            "risk": {"snapshot": risk_dict, "fresh": risk_fresh, "halts": halts,
                     "exposure": total, "by_market": per_market, "by_group": per_group,
                     "market_limit": config.initial_cash * config.max_market_fraction,
                     "event_limit": config.initial_cash * config.max_event_fraction,
                     "total_limit": config.initial_cash * config.max_total_fraction,
                     "daily_loss_limit": config.max_daily_loss_fraction,
                     "drawdown_limit": config.max_drawdown_fraction}}
