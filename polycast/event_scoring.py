"""Unconditional daily settlement scoring, isolated from predictions and orders.

A market midpoint identifies expected token payout, not the three-state class
distribution. Gamma terminal status is cross-checked against the exact MLB
game; cancellation/reschedule cases remain pending without stronger proof.
"""
from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from .event_research import INPUT_VERSION, checked_forecast, checked_settlement_control, instant, sha, audit_daily_schedule
from .market import _list

VERSION = "daily-unconditional-settlement-scoring-v1"


def checked_probabilities(packet: dict, probabilities: dict) -> dict:
    keys = [o["key"] for o in packet["options"]]
    if (not isinstance(probabilities, dict) or set(probabilities) != set(keys) or
            any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                for v in probabilities.values()) or abs(sum(probabilities.values()) - 1) > 1e-6):
        raise ValueError("Probabilities must match the unmodified exhaustive state distribution")
    return dict(probabilities)


def expected_payouts(packet: dict, probabilities: dict) -> dict:
    if packet.get("input_version") != INPUT_VERSION or packet["category"] != "daily":
        raise ValueError("Unconditional daily payout requires a current daily packet")
    probabilities = checked_probabilities(packet, probabilities)
    target = packet["forecast_target"]
    if not target.get("expected_payout_available") or target.get("conditioning") is not None:
        raise ValueError("Conditional winner probabilities cannot determine unconditional payout")
    return {side: sum(probabilities[state] * target["payout_by_state"][state][side]
                      for state in probabilities) for side in ("YES", "NO")}


def score_distribution(packet: dict, probabilities: dict, official_state: str) -> dict:
    probabilities = checked_probabilities(packet, probabilities)
    if official_state not in probabilities:
        raise ValueError("Official state is outside the forecast target")
    probability = probabilities[official_state]
    score = {"scoring_version": VERSION, "official_state": official_state,
             "probabilities": probabilities,
             "multiclass_brier_sum": sum((p - int(state == official_state)) ** 2
                                         for state, p in probabilities.items()),
             "brier_convention": "sum across all states; not divided by the state count",
             "log_loss": -math.log(probability) if probability > 0 else None,
             "log_loss_status": "finite" if probability > 0 else "positive_infinity",
             "probabilities_clipped_or_normalized": False}
    payouts = expected_payouts(packet, probabilities)
    realized = packet["forecast_target"]["payout_by_state"][official_state]
    score.update(expected_payouts=payouts, realized_payouts=realized,
                 yes_payout_squared_error=(payouts["YES"] - realized["YES"]) ** 2,
                 payout_score_limitation="Squared payout error scores the payout mean, not the full state distribution")
    return score


def verify_daily_resolution(packet: dict, gamma: dict, mlb: dict, observed_at: str) -> dict:
    """Verify exact frozen contract + terminal Gamma + independently final game.

    A closed market with 0.5 prices alone is not final resolution proof. A
    postponed/cancelled game can be rescheduled, so it is not automatically HALF.
    No onchain proof or oracle signature is claimed by this public API adapter.
    """
    if packet.get("input_version") != INPUT_VERSION or packet.get("category") != "daily":
        raise ValueError("Only current unconditional daily packets are supported")
    observed = instant(observed_at)
    contract = packet["contract_snapshot"][0]
    pending = {"status": "pending", "scoring_version": VERSION, "event_id": packet["event_id"],
               "observed_at": observed.isoformat(), "contract_sha256": sha(packet["contract_snapshot"]),
               "gamma_sha256": sha(gamma), "mlb_sha256": sha(mlb),
               "onchain_payout_verified": False, "independent_oracle_signature_verified": False}
    def wait(reason):
        return {**pending, "reason": reason}
    if observed <= instant(packet["end_at"]):
        return wait("event_not_ended")
    if audit_daily_schedule(packet)["status"] in ("conflict", "ambiguous_rule_schedule", "invalid_rule_schedule"):
        return wait("contract_rule_time_requires_clarification")
    if str(gamma.get("id")) != packet["event_id"] or gamma.get("slug") != contract["event_slug"]:
        return wait("gamma_event_identity_mismatch")
    matches = [m for m in gamma.get("markets", []) if str(m.get("id")) == contract["id"]]
    if len(matches) != 1:
        return wait("gamma_market_missing_or_duplicate")
    remote = matches[0]
    if (remote.get("description") != contract["description"] or remote.get("question") != contract["question"] or
            _list(remote.get("clobTokenIds")) != [contract["yes_token"], contract["no_token"]] or
            _list(remote.get("outcomes")) != [contract["yes_label"], contract["no_label"]] or
            remote.get("resolutionSource") != "https://www.mlb.com/"):
        return wait("frozen_contract_changed_or_mismatched")
    try:
        game_start = instant(remote["gameStartTime"])
        closed_at = instant(remote.get("closedTime") or gamma.get("closedTime"))
    except (KeyError, ValueError):
        return wait("terminal_or_game_clock_missing")
    if game_start != instant(contract["end_at"]):
        return wait("gamma_game_time_differs_from_frozen_contract")
    if (remote.get("closed") is not True or remote.get("umaResolutionStatus") != "resolved" or
            not instant(packet["end_at"]) < closed_at <= observed - timedelta(minutes=10)):
        return wait("gamma_final_resolution_or_confirmation_pending")
    try:
        raw_prices = _list(remote.get("outcomePrices"))
        if len(raw_prices) != 2 or any(type(v) not in (str, int, float) for v in raw_prices):
            return wait("unsupported_terminal_payout")
        prices = [Decimal(str(v)) for v in raw_prices]
    except (InvalidOperation, ValueError):
        return wait("invalid_terminal_payout")
    if any(not p.is_finite() for p in prices):
        return wait("invalid_terminal_payout")
    states = {(Decimal("1"), Decimal("0")): "YES", (Decimal("0"), Decimal("1")): "NO",
              (Decimal("0.5"), Decimal("0.5")): "HALF"}
    gamma_state = states.get(tuple(prices))
    if gamma_state not in packet["forecast_target"]["states"]:
        return wait("unsupported_terminal_payout")
    games = [g for day in mlb.get("dates", []) for g in day.get("games", [])
             if str(g.get("gamePk")) == contract["source_ref"]]
    if len(games) != 1:
        return wait("mlb_game_missing_or_duplicate")
    game = games[0]
    status = game.get("status", {})
    detail = str(status.get("detailedState", "")).lower()
    if status.get("abstractGameState") != "Final" or any(word in detail for word in ("cancel", "postpon", "suspend", "delay")):
        return wait("mlb_not_final_or_cancellation_needs_reschedule_proof")
    try:
        if instant(game["gameDate"]) != instant(contract["end_at"]):
            return wait("mlb_start_time_differs_from_frozen_contract")
        away, home = game["teams"]["away"], game["teams"]["home"]
        if away["team"]["name"] != contract["yes_label"] or home["team"]["name"] != contract["no_label"]:
            return wait("mlb_team_identity_mismatch")
        scores = [away["score"], home["score"]]
        if any(type(s) is not int or s < 0 for s in scores):
            return wait("mlb_final_score_invalid")
    except (KeyError, ValueError, TypeError):
        return wait("mlb_final_fields_missing")
    mlb_state = "HALF" if scores[0] == scores[1] else "YES" if scores[0] > scores[1] else "NO"
    if mlb_state != gamma_state:
        return wait("independent_result_conflict")
    return {**pending, "status": "verified", "reason": "gamma_final_and_exact_mlb_final_agree",
            "official_state": gamma_state, "closed_at": closed_at.isoformat(),
            "payouts": packet["forecast_target"]["payout_by_state"][gamma_state],
            "verification": "public_gamma_final_status_crosschecked_with_exact_mlb_final",
            "cancelled_game_half_verification_supported": False}


def receipt_probabilities(packet: dict, receipt: dict) -> dict:
    if receipt.get("packet_sha256") != sha(packet) or receipt.get("as_of") != packet["as_of"]:
        raise ValueError("Forecast receipt does not match the frozen packet")
    if receipt.get("status") != "published_research_forecast" or receipt.get("abstain") is not False:
        raise ValueError("Forecast failed, was late, or abstained")
    start, published, cutoff = (instant(receipt[name]) for name in ("started_at", "published_at", "as_of"))
    if not cutoff <= start <= published < instant(packet["end_at"]) or (published - cutoff).total_seconds() > 300:
        raise ValueError("Forecast publication is outside the causal deadline")
    result = receipt.get("result") or {}
    if receipt.get("style") == "structured":
        checked_forecast(packet, result)
        probabilities = result["probabilities"]
    elif receipt.get("style") == "control" and packet["kind"] == "settlement_multiple_choice":
        checked_settlement_control(packet, result)
        probabilities = result["probabilities"]
    elif receipt.get("style") == "control" and packet["kind"] == "binary":
        if result.get("market_id") != packet["market_ids"][0] or result.get("as_of") != packet["as_of"] or result.get("abstain") is not False:
            raise ValueError("Binary control identity differs")
        p = result.get("raw_p_yes", result.get("p_yes"))
        if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("Invalid binary control probability")
        probabilities = {"YES": p, "NO": 1-p}
    else:
        raise ValueError("Unsupported forecast receipt style or target")
    if result.get("abstain") is not False:
        raise ValueError("Forecast result abstained")
    return checked_probabilities(packet, probabilities)


def score_pilot(wrapper: dict, receipts: list[dict], official: dict) -> dict:
    packet = wrapper["packet"]
    if packet.get("input_version") != INPUT_VERSION or packet["category"] != "daily":
        raise ValueError("Legacy conditional packets cannot be upgraded into scored unconditional forecasts")
    if len(receipts) != 2 or {r.get("style") for r in receipts} != {"control", "structured"}:
        raise ValueError("Exactly one receipt per predeclared style is required")
    scores, excluded = {}, {}
    verified = (official.get("status") == "verified" and official.get("event_id") == packet["event_id"] and
                official.get("contract_sha256") == sha(packet["contract_snapshot"]) and
                instant(official["observed_at"]) > instant(packet["end_at"]))
    for receipt in receipts:
        style = receipt["style"]
        if not verified:
            excluded[style] = "official_resolution_not_verified"
            continue
        try:
            probabilities = receipt_probabilities(packet, receipt)
            scores[style] = score_distribution(packet, probabilities, official["official_state"])
        except (ValueError, KeyError, TypeError) as exc:
            excluded[style] = str(exc)
    baseline = {"status": "unavailable", "distribution_comparison_eligible": False}
    market = wrapper.get("market_baselines", {}).get(packet["market_ids"][0], {})
    try:
        mid = market.get("p_yes_mid")
        cutoff = instant(packet["as_of"])
        if (verified and market.get("status") == "captured" and type(mid) in (int, float) and math.isfinite(mid) and 0 <= mid <= 1 and
                all(0 <= (cutoff-instant(market[name])).total_seconds() <= 10 for name in ("received_at", "book_timestamp"))):
            actual = packet["forecast_target"]["payout_by_state"][official["official_state"]]["YES"]
            baseline = {"status": "scored_expected_payout_mean", "expected_yes_payout": mid,
                        "realized_yes_payout": actual, "yes_payout_squared_error": (mid-actual)**2,
                        "distribution_comparison_eligible": packet["kind"] == "binary",
                        "multiclass_brier_sum": None if packet["kind"] != "binary" else 2*(mid-actual)**2,
                        "limitation": "A single price cannot identify P(YES), P(NO), P(HALF) separately"}
    except (ValueError, KeyError, TypeError):
        pass
    return {"schema_version": 1, "scoring_version": VERSION, "event_id": packet["event_id"],
            "packet_sha256": sha(packet), "official_resolution": official,
            "scores": scores, "exclusions": excluded, "market_baseline": baseline,
            "paired_schema_valid_scores": len(scores) == 2,
            "semantic_review_completed": False, "paired_model_selection_eligible": False,
            "prospective_registered": False, "net_profit": None, "live_ready": False,
            "limitations": ["Engineering diagnostic only; semantic review and prospective registration are not complete",
                           "Payout score is not a full probability-distribution comparison or execution profit",
                           "Cancellation/reschedule HALF requires additional independent proof",
                           "Public final status is not independently verified onchain payout"]}

