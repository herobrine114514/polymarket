from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone

from .market import complete_fed_event, parse_utc
from .paper import _risk_group
from .billing import cost_summary


_PROXY_PATTERN = re.compile(
    r"(?:spot-diffusion proxy|normal diffusion baseline) P\(Up\) (0(?:\.\d{4})?|1(?:\.0{4})?)\."
)


def _proxy_from_draft(conn: sqlite3.Connection, forecast: sqlite3.Row,
                      market_id: str, category: str) -> float | None:
    """Use only the deterministic proxy actually present in this forecast's frozen input."""
    source_id = {"crypto5m": "coinbase_btc_5m", "crypto": "binance_klines"}.get(category)
    if source_id is None:
        return None
    draft = conn.execute("""SELECT payload_json FROM analysis_drafts
        WHERE market_id=? AND rules_hash=? AND evidence_hash=? AND as_of=?
        ORDER BY id DESC LIMIT 1""", (market_id, forecast["rules_hash"],
                                       forecast["evidence_hash"], forecast["as_of"])).fetchone()
    if draft is None:
        return None
    try:
        payload = json.loads(draft["payload_json"])
        if payload.get("market_id") != market_id or payload.get("category") != category:
            return None
        forecast_time = parse_utc(forecast["as_of"])
        if forecast_time is None:
            return None
        for item in payload["evidence"]:
            if item.get("source_id") != source_id:
                continue
            published = parse_utc(item.get("published_at"))
            first_seen = parse_utc(item.get("first_seen_at"))
            max_age = 90 if category == "crypto5m" else 120
            if (published is None or first_seen is None or
                    not published <= first_seen <= forecast_time or
                    (forecast_time - published).total_seconds() > max_age):
                continue
            match = _PROXY_PATTERN.search(item.get("summary", ""))
            if match:
                return float(match.group(1))
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    return None


def _rejected_five_minute_shadow(conn: sqlite3.Connection) -> dict:
    """Research-only score for rejected model probabilities; never an order signal."""
    rows = conn.execute("""SELECT p.*,m.event_id,m.end_at,r.winning_side,r.resolved_at,
        (SELECT COUNT(*) FROM markets sibling WHERE sibling.event_id=m.event_id) AS event_markets,
        b.captured_at,b.book_timestamp,b.p_yes_mid AS book_mid,
        b.yes_best_bid AS book_bid,b.yes_best_ask AS book_ask,
        a.captured_at AS staged_at,a.book_timestamp AS staged_book_at,
        a.yes_best_bid AS staged_bid,a.yes_best_ask AS staged_ask,
        d.result_json AS draft_result
        FROM predictions p JOIN markets m ON m.id=p.market_id
        JOIN resolutions r ON r.market_id=p.market_id
        LEFT JOIN market_baselines b ON b.prediction_id=p.id
        LEFT JOIN analysis_market_baselines a ON a.market_id=p.market_id
          AND a.rules_hash=p.rules_hash AND a.evidence_hash=p.evidence_hash
          AND a.as_of=p.as_of
        LEFT JOIN analysis_drafts d ON d.market_id=p.market_id
          AND d.rules_hash=p.rules_hash AND d.evidence_hash=p.evidence_hash AND d.as_of=p.as_of
        WHERE m.category='crypto5m'""").fetchall()
    chosen: dict[str, dict] = {}
    # ISO strings with different offsets do not sort chronologically.
    rows = sorted(rows, key=lambda r: (parse_utc(r["as_of"]) or
                  datetime.min.replace(tzinfo=timezone.utc), r["id"]), reverse=True)
    for row in rows:
        # Supported BTC 5m events have exactly one binary market. Do not count
        # an unexpected multi-market event as several independent experiments.
        if row["event_markets"] != 1 or row["event_id"] in chosen:
            continue
        try:
            output = json.loads(row["model_output_json"])
            raw = output.get("p_yes")
            analysis_abstain = output.get("analysis_abstain")
            provenance = "saved_analysis_flag"
            if "analysis_abstain" not in output and row["draft_result"]:
                draft = json.loads(row["draft_result"])
                # Recover legacy flags only from the exact frozen analysis, not
                # from the final abstain forced by a rejection.
                if (draft.get("market_id") == row["market_id"] and
                        draft.get("as_of") == row["as_of"] and
                        draft.get("p_yes") == raw):
                    analysis_abstain = draft.get("abstain")
                    provenance = "frozen_analysis_draft"
            forecast_at = parse_utc(row["as_of"])
            reviewed_at = parse_utc(output.get("reviewed_at"))
            resolved_at = parse_utc(row["resolved_at"])
            end_at = parse_utc(row["end_at"])
            if (analysis_abstain is not False or
                    output.get("review", {}).get("approved") is not False or
                    isinstance(raw, bool) or not isinstance(raw, (int, float)) or
                    not math.isfinite(raw) or not 0 <= raw <= 1 or
                    not forecast_at or not reviewed_at or not resolved_at or not end_at or
                    not forecast_at <= reviewed_at < min(resolved_at, end_at)):
                continue
            outcome = float(row["winning_side"] == "YES")
            item = {"market_id": row["market_id"], "event_id": row["event_id"],
                    "prediction_id": row["id"], "analysis_flag_source": provenance,
                    "prompt_version": output.get("prompt_version") or "unknown",
                    "model_version": output.get("model_version") or "unknown",
                    "p_yes": float(raw), "brier": (float(raw) - outcome) ** 2,
                    "market_p_yes": None, "market_brier": None}
            captured_at = parse_utc(row["captured_at"])
            book_at = parse_utc(row["book_timestamp"])
            prices = [row[k] for k in ("book_bid", "book_ask", "staged_bid", "staged_ask", "book_mid")]
            valid_prices = all(isinstance(x, (int, float)) and math.isfinite(x) and
                               0 < x < 1 for x in prices)
            if (valid_prices and row["book_bid"] <= row["book_ask"] and
                    row["book_bid"] == row["staged_bid"] and
                    row["book_ask"] == row["staged_ask"] and
                    math.isclose(row["book_mid"], (row["book_bid"] + row["book_ask"]) / 2,
                                 rel_tol=0, abs_tol=1e-9) and
                    captured_at and book_at and parse_utc(row["staged_at"]) == captured_at and
                    parse_utc(row["staged_book_at"]) == book_at and
                    0 <= (forecast_at - captured_at).total_seconds() <= 10 and
                    0 <= (captured_at - book_at).total_seconds() <= 10):
                market_p = float(row["book_mid"])
                item["market_p_yes"] = market_p
                item["market_brier"] = (market_p - outcome) ** 2
            chosen[row["event_id"]] = item
        except (TypeError, ValueError, AttributeError, KeyError):
            continue
    values = list(chosen.values())
    paired = [item for item in values if item["market_brier"] is not None]
    def cohort(items: list[dict]) -> dict:
        own = items
        own_paired = [item for item in own if item["market_brier"] is not None]
        return {"events": len(own), "paired_events": len(own_paired),
                            "model_brier": round(sum(x["brier"] for x in own) / len(own), 6),
                            "paired_model_brier": round(sum(x["brier"] for x in own_paired) /
                                                        len(own_paired), 6) if own_paired else None,
                            "paired_market_brier": round(sum(x["market_brier"] for x in own_paired) /
                                                         len(own_paired), 6) if own_paired else None}
    cohorts = {version: cohort([x for x in values if x["prompt_version"] == version])
               for version in sorted({x["prompt_version"] for x in values})}
    models = {version: cohort([x for x in values if x["model_version"] == version])
              for version in sorted({x["model_version"] for x in values})}
    joint = {model: {version: cohort([x for x in values if x["model_version"] == model and
                                     x["prompt_version"] == version])
                    for version in sorted({x["prompt_version"] for x in values if x["model_version"] == model})}
             for model in models}
    return {"scope": "Rejected analysis-only BTC 5m forecasts; latest eligible rejection per single-market event before window end; no paper order or validation credit",
            "selection_bias": "Rejected forecasts are a selected diagnostic cohort; do not pool with approved forecasts or interpret as trading performance",
            "excluded_multi_market_events": len({r["event_id"] for r in rows if r["event_markets"] != 1}),
            "events": len(values), "paired_events": len(paired),
            "model_brier": round(sum(x["brier"] for x in values) / len(values), 6) if values else None,
            "paired_model_brier": round(sum(x["brier"] for x in paired) / len(paired), 6) if paired else None,
            "paired_market_brier": round(sum(x["market_brier"] for x in paired) / len(paired), 6) if paired else None,
            "by_prompt_version": cohorts,
            "by_model": models, "by_model_and_prompt": joint,
            "markets": [{**item, "brier": round(item["brier"], 6),
                         "market_brier": round(item["market_brier"], 6)
                         if item["market_brier"] is not None else None} for item in values]}


def report(conn: sqlite3.Connection) -> dict:
    """Score only forecasts saved before a sourced, verified outcome."""
    resolutions = conn.execute("""SELECT r.market_id,r.winning_side,r.source_url,r.resolved_at,
        m.event_id,m.event_title,m.question,m.category FROM resolutions r
        JOIN markets m ON m.id=r.market_id ORDER BY r.resolved_at,r.market_id""").fetchall()
    event_cutoffs: dict[str, datetime] = {}
    for resolved in resolutions:
        at = parse_utc(resolved["resolved_at"])
        previous = event_cutoffs.get(resolved["event_id"])
        if at and (previous is None or at < previous):
            event_cutoffs[resolved["event_id"]] = at
    event_markets: dict[str, list[dict]] = {}
    for resolved in resolutions:
        if resolved["event_id"] not in event_markets:
            event_markets[resolved["event_id"]] = [dict(row) for row in conn.execute(
                "SELECT id,question FROM markets WHERE event_id=? ORDER BY id",
                (resolved["event_id"],))]
    groups: dict[str, dict] = {}
    market_results: list[dict] = []
    reviewed_markets = 0
    abstained_markets = 0
    for resolved in resolutions:
        # Risk caps may aggregate all Fed events; validation counts each meeting once.
        group_id = f"{_risk_group(resolved)}:event:{resolved['event_id']}"
        group = groups.setdefault(group_id, {"risk_group": group_id, "event_ids": set(),
                                             "resolved_markets": 0, "scored_markets": 0,
                                             "brier_sum": 0.0, "log_loss_sum": 0.0,
                                             "market_baseline_count": 0,
                                             "market_baseline_brier_sum": 0.0,
                                             "matched_model_brier_sum": 0.0,
                                             "pre_model_baseline_count": 0,
                                             "pre_model_baseline_brier_sum": 0.0,
                                             "pre_model_matched_model_brier_sum": 0.0,
                                             "pre_model_market_probabilities": {},
                                             "proxy_baseline_count": 0,
                                             "proxy_baseline_brier_sum": 0.0,
                                             "proxy_matched_model_brier_sum": 0.0,
                                             "proxy_probabilities": {},
                                             "realized_pnl": 0.0,
                                             "resolved_ids": set(), "scored_ids": set(),
                                             "joint_ids": set(), "probabilities": {},
                                             "providers": set(),
                                             "models": set(),
                                             "market_probabilities": {},
                                             "yes_winners": []})
        group["event_ids"].add(resolved["event_id"])
        group["resolved_markets"] += 1
        group["resolved_ids"].add(resolved["market_id"])
        if resolved["winning_side"] == "YES":
            group["yes_winners"].append(resolved["market_id"])
        resolution_time = event_cutoffs.get(resolved["event_id"])
        prediction = None
        forecasts = conn.execute("""SELECT id,as_of,rules_hash,evidence_hash,
            probability,abstain,model_output_json
            FROM predictions WHERE market_id=?""", (resolved["market_id"],)).fetchall()
        forecasts = sorted(forecasts, key=lambda r: (parse_utc(r["as_of"]) or
                           datetime.min.replace(tzinfo=timezone.utc), r["id"]), reverse=True)
        for forecast in forecasts:
            try:
                output = json.loads(forecast["model_output_json"])
                review_time = parse_utc(output.get("reviewed_at"))
                forecast_time = parse_utc(forecast["as_of"])
                approved = output.get("review", {}).get("approved") is True
            except (TypeError, ValueError, AttributeError):
                continue
            if (approved and forecast_time and review_time and resolution_time and
                    forecast_time <= review_time <= resolution_time):
                prediction = forecast
                break
        pnl = conn.execute("""SELECT COALESCE(SUM(shares*settlement-total_cost),0)
            FROM paper_orders WHERE market_id=? AND status='settled'""",
            (resolved["market_id"],)).fetchone()[0]
        group["realized_pnl"] += float(pnl)
        result = {"market_id": resolved["market_id"], "event_id": resolved["event_id"],
                  "question": resolved["question"], "risk_group": group_id,
                  "winning_side": resolved["winning_side"], "resolved_at": resolved["resolved_at"],
                  "source_url": resolved["source_url"], "prediction_id": None,
                  "p_yes_raw": None, "brier": None, "log_loss": None,
                  "market_p_yes_mid": None, "market_brier": None,
                  "market_baseline_phase": None, "market_baseline_delay_seconds": None,
                  "proxy_p_yes": None, "proxy_brier": None,
                  "provider": None, "model_version": None,
                  "reviewed": bool(prediction),
                  "abstained": bool(prediction and prediction["abstain"]),
                  "realized_pnl": round(float(pnl), 6)}
        if prediction:
            result["provider"] = (output.get("provider")
                                  if output.get("provider") in ("codex", "deepseek")
                                  else "unknown")
            version = output.get("model_version")
            result["model_version"] = version if isinstance(version, str) and version else "unknown"
            reviewed_markets += 1
            abstained_markets += int(bool(prediction["abstain"]))
        if prediction and not prediction["abstain"] and prediction["probability"] is not None:
            probability = float(prediction["probability"])
            outcome = 1.0 if resolved["winning_side"] == "YES" else 0.0
            brier = (probability - outcome) ** 2
            likelihood = probability if outcome else 1 - probability
            log_loss = -math.log(max(1e-12, likelihood))
            group["scored_markets"] += 1
            group["scored_ids"].add(resolved["market_id"])
            group["probabilities"][resolved["market_id"]] = probability
            group["providers"].add(result["provider"])
            group["models"].add(result["model_version"])
            group["joint_ids"].add(output.get("joint_forecast_id"))
            group["brier_sum"] += brier
            group["log_loss_sum"] += log_loss
            result.update({"prediction_id": int(prediction["id"]), "p_yes_raw": probability,
                           "brier": round(brier, 6), "log_loss": round(log_loss, 6)})
            proxy = _proxy_from_draft(conn, prediction, resolved["market_id"], resolved["category"])
            if proxy is not None:
                proxy_brier = (proxy - outcome) ** 2
                group["proxy_baseline_count"] += 1
                group["proxy_baseline_brier_sum"] += proxy_brier
                group["proxy_matched_model_brier_sum"] += brier
                group["proxy_probabilities"][resolved["market_id"]] = proxy
                result.update({"proxy_p_yes": proxy, "proxy_brier": round(proxy_brier, 6)})
            baseline = conn.execute("""SELECT captured_at,book_timestamp,p_yes_mid FROM market_baselines
                WHERE prediction_id=?""", (prediction["id"],)).fetchone()
            if baseline:
                capture_time = parse_utc(baseline["captured_at"])
                book_time = parse_utc(baseline["book_timestamp"])
                staged = conn.execute("""SELECT captured_at,book_timestamp,
                    (yes_best_bid+yes_best_ask)/2 AS p_yes_mid
                    FROM analysis_market_baselines WHERE market_id=? AND rules_hash=?
                    AND evidence_hash=? AND as_of=?""",
                    (resolved["market_id"], prediction["rules_hash"],
                     prediction["evidence_hash"], prediction["as_of"])).fetchone()
                pre_model = bool(staged and capture_time and book_time and forecast_time and review_time and
                                 staged["captured_at"] == baseline["captured_at"] and
                                 staged["book_timestamp"] == baseline["book_timestamp"] and
                                 math.isclose(staged["p_yes_mid"], baseline["p_yes_mid"], abs_tol=1e-9) and
                                 0 <= (forecast_time - capture_time).total_seconds() <= 10 and
                                 0 <= (capture_time - book_time).total_seconds() <= 10 and
                                 capture_time <= review_time)
                post_review = bool(capture_time and review_time and resolution_time and
                                   review_time <= capture_time <= resolution_time)
                if pre_model or post_review:
                    market_probability = float(baseline["p_yes_mid"])
                    market_brier = (market_probability - outcome) ** 2
                    group["market_baseline_count"] += 1
                    group["market_probabilities"][resolved["market_id"]] = market_probability
                    group["market_baseline_brier_sum"] += market_brier
                    group["matched_model_brier_sum"] += brier
                    if pre_model:
                        group["pre_model_baseline_count"] += 1
                        group["pre_model_baseline_brier_sum"] += market_brier
                        group["pre_model_matched_model_brier_sum"] += brier
                        group["pre_model_market_probabilities"][resolved["market_id"]] = market_probability
                    result.update({"market_p_yes_mid": market_probability,
                                   "market_brier": round(market_brier, 6),
                                   "market_baseline_phase": "pre_model" if pre_model else "post_review",
                                   "market_baseline_delay_seconds": round(
                                       (capture_time - forecast_time).total_seconds(), 3)})
        market_results.append(result)

    group_results = []
    for group in groups.values():
        event_id = next(iter(group["event_ids"]))
        expected_ids = {row["id"] for row in event_markets[event_id]}
        joint_fed = complete_fed_event(event_markets[event_id])
        count = group["scored_markets"]
        complete = (bool(expected_ids) and group["resolved_ids"] == expected_ids and
                    group["scored_ids"] == expected_ids)
        if joint_fed:
            complete = (complete and len(group["joint_ids"]) == 1 and
                        None not in group["joint_ids"] and
                        len(group["yes_winners"]) == 1 and
                        math.isclose(sum(group["probabilities"].values()), 1.0,
                                     rel_tol=0, abs_tol=1e-6))
        baseline_complete = complete and set(group["market_probabilities"]) == expected_ids
        pre_model_complete = complete and set(group["pre_model_market_probabilities"]) == expected_ids
        proxy_complete = complete and set(group["proxy_probabilities"]) == expected_ids
        provider = (next(iter(group["providers"])) if len(group["providers"]) == 1
                    else "mixed" if group["providers"] else None)
        model_version = (next(iter(group["models"])) if len(group["models"]) == 1
                         else "mixed" if group["models"] else None)
        joint_brier = joint_log_loss = None
        joint_market_brier = joint_market_log_loss = None
        market_price_sum = None
        if joint_fed and complete:
            winner = group["yes_winners"][0]
            joint_brier = sum((probability - float(market_id == winner)) ** 2
                              for market_id, probability in group["probabilities"].items())
            joint_log_loss = -math.log(max(1e-12, group["probabilities"][winner]))
            if baseline_complete:
                market_price_sum = sum(group["market_probabilities"].values())
                if market_price_sum > 0:
                    normalized = {market_id: probability / market_price_sum
                                  for market_id, probability in group["market_probabilities"].items()}
                    joint_market_brier = sum((probability - float(market_id == winner)) ** 2
                                             for market_id, probability in normalized.items())
                    joint_market_log_loss = -math.log(max(1e-12, normalized[winner]))
        group_results.append({"risk_group": group["risk_group"],
                              "provider": provider,
                              "model_version": model_version,
                              "event_ids": sorted(group["event_ids"]),
                              "expected_markets": len(expected_ids),
                              "resolved_markets": group["resolved_markets"],
                              "scored_markets": count,
                              "complete_for_validation": bool(complete),
                              "baseline_complete_for_validation": bool(baseline_complete),
                              "pre_model_baseline_complete_for_validation": bool(pre_model_complete),
                              "proxy_complete_for_validation": bool(proxy_complete),
                              "joint_fed_event": joint_fed,
                              "joint_brier": round(joint_brier, 6) if joint_brier is not None else None,
                              "joint_log_loss": round(joint_log_loss, 6) if joint_log_loss is not None else None,
                              "joint_market_brier": round(joint_market_brier, 6)
                              if joint_market_brier is not None else None,
                              "joint_market_log_loss": round(joint_market_log_loss, 6)
                              if joint_market_log_loss is not None else None,
                              "market_price_sum": round(market_price_sum, 6)
                              if market_price_sum is not None else None,
                              "brier_mean": round(group["brier_sum"] / count, 6) if count else None,
                              "log_loss_mean": round(group["log_loss_sum"] / count, 6) if count else None,
                              "market_baseline_scored_markets": group["market_baseline_count"],
                              "market_baseline_brier_mean": round(
                                  group["market_baseline_brier_sum"] / group["market_baseline_count"], 6
                              ) if group["market_baseline_count"] else None,
                              "matched_model_brier_mean": round(
                                  group["matched_model_brier_sum"] / group["market_baseline_count"], 6
                              ) if group["market_baseline_count"] else None,
                              "pre_model_baseline_scored_markets": group["pre_model_baseline_count"],
                              "pre_model_market_brier_mean": round(
                                  group["pre_model_baseline_brier_sum"] / group["pre_model_baseline_count"], 6
                              ) if group["pre_model_baseline_count"] else None,
                              "pre_model_matched_model_brier_mean": round(
                                  group["pre_model_matched_model_brier_sum"] / group["pre_model_baseline_count"], 6
                              ) if group["pre_model_baseline_count"] else None,
                              "proxy_baseline_scored_markets": group["proxy_baseline_count"],
                              "proxy_baseline_brier_mean": round(
                                  group["proxy_baseline_brier_sum"] / group["proxy_baseline_count"], 6
                              ) if group["proxy_baseline_count"] else None,
                              "proxy_matched_model_brier_mean": round(
                                  group["proxy_matched_model_brier_sum"] / group["proxy_baseline_count"], 6
                              ) if group["proxy_baseline_count"] else None,
                              "realized_pnl": round(group["realized_pnl"], 6)})
    group_results.sort(key=lambda group: group["risk_group"])
    scored_groups = [group for group in group_results if group["complete_for_validation"]]
    matched_groups = [group for group in group_results if group["baseline_complete_for_validation"]]
    pre_model_groups = [group for group in group_results if group["pre_model_baseline_complete_for_validation"]]
    proxy_groups = [group for group in group_results if group["proxy_complete_for_validation"]]
    def cohort(label: str, field: str) -> dict:
        own = [group for group in group_results
               if group["complete_for_validation"] and group[field] == label]
        matched = [group for group in own if group["baseline_complete_for_validation"]]
        pre_model = [group for group in own if group["pre_model_baseline_complete_for_validation"]]
        return {
            "reviewed_pre_outcome_markets": sum(
                1 for result in market_results
                if result[field] == label and result["reviewed"]),
            "abstained_markets": sum(
                1 for result in market_results
                if result[field] == label and result["abstained"]),
            "complete_scored_events": len(own),
            "complete_matched_events": len(matched),
            "complete_pre_model_matched_events": len(pre_model),
            "risk_group_weighted_brier": round(
                sum(group["brier_mean"] for group in own) / len(own), 6) if own else None,
            "risk_group_weighted_matched_model_brier": round(
                sum(group["matched_model_brier_mean"] for group in matched) / len(matched), 6)
                if matched else None,
            "risk_group_weighted_market_brier": round(
                sum(group["market_baseline_brier_mean"] for group in matched) / len(matched), 6)
                if matched else None,
            "risk_group_weighted_pre_model_matched_model_brier": round(
                sum(group["pre_model_matched_model_brier_mean"] for group in pre_model) / len(pre_model), 6)
                if pre_model else None,
            "risk_group_weighted_pre_model_market_brier": round(
                sum(group["pre_model_market_brier_mean"] for group in pre_model) / len(pre_model), 6)
                if pre_model else None,
        }
    by_provider = {provider: cohort(provider, "provider")
                   for provider in ("codex", "deepseek", "unknown", "mixed")}
    model_names = {result["model_version"] for result in market_results
                   if result["model_version"]}
    model_names.update(group["model_version"] for group in group_results
                       if group["model_version"])
    model_names.update(("deepseek-flash", "deepseek-v4-pro"))
    by_model = {model: cohort(model, "model_version") for model in sorted(model_names)}
    return {"score_type": "raw, uncalibrated, complete-event forecasts reviewed before the first event outcome; pre-model books reported separately from historical post-review books",
            "rejected_five_minute_shadow": _rejected_five_minute_shadow(conn),
            "provider_comparison_scope": "Latest approved forecast per resolved event; provider cohorts may contain different events and are not a same-event head-to-head test",
            "model_comparison_scope": "Latest approved forecast per resolved event; model-version cohorts may contain different events and are not a same-event head-to-head test",
            "by_provider": by_provider,
            "by_model": by_model,
            "resolved_markets": len(resolutions),
            "reviewed_pre_outcome_markets": reviewed_markets,
            "abstained_markets": abstained_markets,
            "scored_markets": sum(group["scored_markets"] for group in group_results),
            "market_baseline_scored_markets": sum(
                group["market_baseline_scored_markets"] for group in group_results),
            "proxy_baseline_scored_markets": sum(
                group["proxy_baseline_scored_markets"] for group in group_results),
            "risk_groups": len(groups), "scored_risk_groups": len(scored_groups),
            "complete_scored_events": len(scored_groups),
            "complete_matched_events": len(matched_groups),
            "complete_pre_model_matched_events": len(pre_model_groups),
            "complete_proxy_matched_events": len(proxy_groups),
            "risk_group_weighted_brier": round(sum(group["brier_mean"] for group in scored_groups) /
                                               len(scored_groups), 6) if scored_groups else None,
            "risk_group_weighted_log_loss": round(sum(group["log_loss_mean"] for group in scored_groups) /
                                                  len(scored_groups), 6) if scored_groups else None,
            "risk_group_weighted_matched_model_brier": round(
                sum(group["matched_model_brier_mean"] for group in matched_groups) /
                len(matched_groups), 6) if matched_groups else None,
            "risk_group_weighted_market_brier": round(
                sum(group["market_baseline_brier_mean"] for group in matched_groups) /
                len(matched_groups), 6) if matched_groups else None,
            "risk_group_weighted_pre_model_matched_model_brier": round(
                sum(group["pre_model_matched_model_brier_mean"] for group in pre_model_groups) /
                len(pre_model_groups), 6) if pre_model_groups else None,
            "risk_group_weighted_pre_model_market_brier": round(
                sum(group["pre_model_market_brier_mean"] for group in pre_model_groups) /
                len(pre_model_groups), 6) if pre_model_groups else None,
            "risk_group_weighted_proxy_matched_model_brier": round(
                sum(group["proxy_matched_model_brier_mean"] for group in proxy_groups) /
                len(proxy_groups), 6) if proxy_groups else None,
            "risk_group_weighted_proxy_brier": round(
                sum(group["proxy_baseline_brier_mean"] for group in proxy_groups) /
                len(proxy_groups), 6) if proxy_groups else None,
            "realized_paper_pnl": round(sum(group["realized_pnl"] for group in group_results), 6),
            "model_costs": cost_summary(conn),
            "initial_review_groups": 100, "initial_review_sample_reached": len(scored_groups) >= 100,
            "groups": group_results, "markets": market_results}
