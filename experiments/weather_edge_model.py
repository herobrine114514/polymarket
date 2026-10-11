"""Can archived public forecasts beat the Polymarket price on daily-high temperature? Offline.

A walk-forward forecast of each station's daily maximum is turned into bucket
probabilities and compared with the public Polymarket price at `--decision-hour` local
time one day (`lead1`) and two days (`lead2`) before the market's day. Only valid hours up
to the decision hour enter the forecast maximum, so every value used had been published
24 h (lead1) or 48 h (lead2) before its valid hour, i.e. no later than the decision time.
Bias, weights and spread come only from station days that were fully observed by then.
`baseline`: equal-weight model mean, trailing bias, Gaussian. `weighted`: per-model bias,
inverse-MSE weights, spread scaled by how wrong past forecasts were at a similar level of
model disagreement, Student-t tails.
Costs: taker fee rate*c*(1-c) plus an assumed half spread; fills are an assumption.
Run in .polycast/oss-numeric-venv. Nothing here places or routes orders.
"""
from __future__ import annotations
import argparse
import bisect
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.special import expit, logit
from scipy.stats import norm, t as student

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.btc5m_backfill import read_rows
from experiments.weather_edge import ALL_MODELS as MODELS, shift_date, weather_events

TAIL_DF = 5                    # forecast errors have heavier tails than a Gaussian
MINIMUM_MODELS = 3
KINDS = ("baseline", "weighted")
# Candidate settings for the weighted mean; the choice is made on days before the first market only.
GRID = [{"window": window, "half_life": half_life, "power": power, "ar": ar}
        for window, half_life in ((45, None), (90, None), (90, 5), (90, 10), (90, 20))
        for power in (1.0, 2.0, 4.0) for ar in (0.0, .2, .4)]
DEFAULT = {"window": 45, "half_life": None, "power": 1.0, "ar": 0.0}


def daily_forecasts(station: dict, decision_hour: int) -> dict[str, dict]:
    """Per local day and lead: each model's maximum over valid hours 00..decision_hour,
    and the epoch of the decision time (that hour, `lead` days earlier)."""
    epoch_of = dict(zip(station["local_time"], station["epoch"]))
    days: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    for index, stamp in enumerate(station["local_time"]):
        if int(stamp[11:13]) > decision_hour:
            continue
        for key, series in station["forecast"].items():
            if series[index] is not None:
                days[stamp[:10]][key].append(series[index])
    result = {}
    for date, values in days.items():
        entry = {}
        for lead in (1, 2):
            maxima = [max(values[key]) if len(values.get(key := f"temperature_2m_previous_day{lead}_{model}", ())) >= decision_hour
                      else math.nan for model in MODELS]           # a model without coverage here stays NaN
            decision = epoch_of.get(f"{shift_date(date, -lead)}T{decision_hour:02d}:00")
            if sum(not math.isnan(value) for value in maxima) >= MINIMUM_MODELS and decision is not None:
                entry[f"lead{lead}"] = maxima
                entry[f"lead{lead}_decision"] = decision
        result[date] = entry
    return result


def walk_forward_means(targets: dict[str, list[float]], observed: dict[str, float], lead: int,
                       minimum: int, config: dict = DEFAULT, partial: dict[str, float] | None = None) -> dict[str, dict]:
    """Predicted daily maximum for every target date from earlier observed days only.

    At the decision time for date D the last fully observed day is D-lead-1, so only
    observed dates < D-lead are used; D's own observation is never read here.
    `half_life` weights recent days more, `power` sharpens the inverse-MSE model weights,
    and `ar` adds that share of the decision day's own error so far (`partial` is the
    maximum observed up to the decision hour, which is known when the forecast is made).
    """
    history = sorted(date for date in observed if date in targets)
    past_observed = np.asarray([observed[date] for date in history])
    past_forecast = np.asarray([targets[date] for date in history]).reshape(len(history), len(MODELS))
    result = {}
    for date, forecast in targets.items():
        decision_day = shift_date(date, -lead)
        stop = bisect.bisect_left(history, decision_day)
        start = max(0, stop - config["window"])
        if stop - start < minimum:
            continue
        truth, model, current = past_observed[start:stop], past_forecast[start:stop], np.asarray(forecast)
        usable = ~np.isnan(current) & (np.isnan(model).mean(axis=0) <= .1)
        days = ~np.isnan(model[:, usable]).any(axis=1)             # past days on which every usable model reported
        if usable.sum() < MINIMUM_MODELS or days.sum() < minimum:
            continue
        truth, model, current = truth[days], model[days][:, usable], current[usable]
        age = np.arange(len(truth))[::-1]
        recency = np.ones(len(truth)) if config["half_life"] is None else .5 ** (age / config["half_life"])
        recency = recency / recency.sum()
        bias = recency @ (truth[:, None] - model)
        weight = np.maximum(recency @ (truth[:, None] - model - bias) ** 2, .05) ** -config["power"]
        weight /= weight.sum()
        weighted = float(weight @ (current + bias))
        if config["ar"] and partial is not None and decision_day in partial and decision_day in targets:
            today = np.asarray(targets[decision_day])[usable]
            if not np.isnan(today).any():
                weighted += config["ar"] * float(partial[decision_day] - weight @ (today + bias))
        flat = slice(max(0, len(truth) - 45), None)
        result[date] = {"baseline": float(current.mean() + (truth[flat] - model[flat].mean(axis=1)).mean()),
                        "weighted": weighted, "spread": float((current + bias).std()), "models": int(usable.sum())}
    return result


def select_config(targets: dict[str, dict], observed: dict[str, dict], partial: dict[str, dict], lead: int,
                  minimum: int, before: str) -> tuple[dict, list[dict]]:
    """Lowest pooled MAE over station days before `before` (the first market day); no market data involved."""
    table = []
    for config in GRID:
        errors = [observed[name][date] - day["weighted"]
                  for name in targets
                  for date, day in walk_forward_means(targets[name], observed[name], lead, minimum, config, partial[name]).items()
                  if date < before and date in observed[name]]
        table.append({**config, "station_days": len(errors), "mae_c": float(np.mean(np.abs(errors)))})
    best = min(table, key=lambda row: row["mae_c"])
    return {key: best[key] for key in DEFAULT}, sorted(table, key=lambda row: row["mae_c"])


class Residuals:
    """Out-of-sample forecast errors, queryable for 'everything known before a date'."""

    def __init__(self, items: list[tuple[str, str, float, float]]):
        items = sorted(items)                       # (date, station, model spread, error)
        self.dates = [item[0] for item in items]
        self.stations = [item[1] for item in items]
        self.spreads = np.asarray([item[2] for item in items])
        self.squared = np.asarray([item[3] ** 2 for item in items])

    def before(self, cutoff: str) -> int:
        return bisect.bisect_left(self.dates, cutoff)

    def scale(self, station: str, cutoff: str, window: int, minimum: int, spread: float | None) -> float | None:
        stop = self.before(cutoff)
        if stop < 300:
            return None                              # too little evidence about error size yet
        everyone = float(self.squared[:stop].mean())
        own = [self.squared[index] for index in range(stop) if self.stations[index] == station][-window:]
        variance = .5 * float(np.mean(own)) + .5 * everyone if len(own) >= minimum else everyone
        if spread is not None:
            # Past errors of the quarter of forecasts whose model disagreement was closest to this one.
            order = np.argsort(self.spreads[:stop], kind="stable")
            ranked = self.spreads[:stop][order]
            centre = int(np.searchsorted(ranked, spread, "left") + np.searchsorted(ranked, spread, "right")) // 2
            low = min(max(0, centre - stop // 8), stop - stop // 4)
            variance *= float(self.squared[:stop][order[low:low + stop // 4]].mean()) / everyone
        return max(.5, math.sqrt(variance))


def bucket_probability(kind: str, low: float, high: float, location: float, scale: float) -> float:
    if kind == "baseline":
        value = norm.cdf(high, location, scale) - norm.cdf(low, location, scale)
    else:
        unit = scale * math.sqrt((TAIL_DF - 2) / TAIL_DF)     # Student-t with the same standard deviation
        value = student.cdf((high - location) / unit, TAIL_DF) - student.cdf((low - location) / unit, TAIL_DF)
    return min(.999, max(.001, float(value)))


def cluster_ci(values: np.ndarray, clusters: np.ndarray, rng: np.random.Generator, draws: int) -> list[float]:
    _, inverse = np.unique(clusters, return_inverse=True)
    count = inverse.max() + 1
    total, size = np.bincount(inverse, weights=values), np.bincount(inverse).astype(float)
    picks = rng.integers(0, count, size=(draws, count))
    return [float(value) for value in np.percentile(total[picks].sum(axis=1) / size[picks].sum(axis=1), [2.5, 97.5])]


STACK_MARKET_SHARES = (1.0, .85, .7, .5, .3, 0.0)   # log-linear weight on the market, rest on the model
STACK_SHARPEN = (1.0, 1.15, 1.3)                    # exponent applied to the pooled distribution
STACK_MINIMUM_EVENTS = 150


def event_level(part: list[dict], rng: np.random.Generator, draws: int, half_spread: float) -> dict:
    """Score whole events (every bucket priced) as distributions, and stack model with market online.

    The stack is a log-linear pool sharpened by an exponent. For each event the pool settings
    are the ones with the lowest log loss over events of strictly earlier market days whose
    outcome was known at this event's decision time; until enough of those exist it simply
    equals the normalized market.
    """
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in part:
        grouped[row["event"]].append(row)
    events = []
    for rows in grouped.values():
        if len(rows) != rows[0]["buckets"] or sum(row["y"] for row in rows) != 1:
            continue                                   # a bucket had no price at the decision time
        rows = sorted(rows, key=lambda row: row["low"])
        raw = np.asarray([row["price"] for row in rows])
        model = np.asarray([row["weighted"] for row in rows])
        events.append({"date": rows[0]["date"], "y": np.asarray([row["y"] for row in rows]), "raw": raw,
                       "market": raw / raw.sum(), "model": model / model.sum(), "sum": float(raw.sum()), "rows": rows})
    events.sort(key=lambda event: event["date"])
    if len(events) < 60:
        return {"events": len(events), "skipped": "too few fully priced events"}
    lead = part[0]["lead"]
    settings = [(share, power) for share in STACK_MARKET_SHARES for power in STACK_SHARPEN]

    def pooled(event: dict, share: float, power: float) -> np.ndarray:
        value = np.exp(power * (share * np.log(event["market"]) + (1 - share) * np.log(event["model"])))
        return value / value.sum()

    losses = np.asarray([[-np.log(max(1e-4, float(pooled(event, *setting)[event["y"] == 1][0]))) for setting in settings]
                         for event in events])
    chosen = []
    market_only = [index for index, setting in enumerate(settings) if setting[0] == 1.0]
    for index, event in enumerate(events):
        known = [other for other in range(index) if events[other]["date"] < shift_date(event["date"], -lead)]
        ready = len(known) >= STACK_MINIMUM_EVENTS
        chosen.append(settings[int(np.argmin(losses[known].mean(axis=0)))] if ready else (1.0, 1.0))
        event["stack"] = pooled(event, *chosen[-1])
        # Ablation: the same online choice restricted to the market alone (sharpening only, no model).
        sharpened = settings[market_only[int(np.argmin(losses[known][:, market_only].mean(axis=0)))]] if ready else (1.0, 1.0)
        event["sharpened"] = pooled(event, *sharpened)
    scored = [index for index in range(len(events)) if chosen[index] != (1.0, 1.0) or
              len([other for other in range(index) if events[other]["date"] < shift_date(events[index]["date"], -lead)]) >= STACK_MINIMUM_EVENTS]
    if len(scored) < 30:
        return {"events": len(events), "skipped": "too few events after the stack warm-up"}
    days = np.asarray([events[index]["date"] for index in scored])

    def metrics(key: str) -> dict:
        values = {"rps": [], "log_loss": [], "brier": []}
        for index in scored:
            event = events[index]
            probability = event[key]
            values["rps"].append(float(np.sum((np.cumsum(probability) - np.cumsum(event["y"])) ** 2)))
            values["log_loss"].append(-float(np.log(max(1e-4, float(probability[event["y"] == 1][0])))))
            values["brier"].append(float(np.mean((probability - event["y"]) ** 2)))
        return {name: np.asarray(series) for name, series in values.items()}

    table = {key: metrics(key) for key in ("raw", "market", "sharpened", "model", "stack")}
    result = {"events": len(events), "events_scored_after_warmup": len(scored), "days": int(len(np.unique(days))),
              "mean_sum_of_bucket_mids": float(np.mean([events[index]["sum"] for index in scored])),
              "final_stack_setting": {"market_share": chosen[-1][0], "sharpen": chosen[-1][1]},
              "stack_settings_used": {f"{share}/{power}": chosen.count((share, power)) for share, power in set(chosen)},
              "scores": {key: {name: float(series.mean()) for name, series in value.items()} for key, value in table.items()},
              "versus_normalized_market": {}, "versus_raw_midpoint": {}}
    result["stack_versus_sharpened_market"] = {
        name: {"difference": float((table["stack"][name] - table["sharpened"][name]).mean()),
               "ci95_by_day": cluster_ci(table["stack"][name] - table["sharpened"][name], days, rng, draws)}
        for name in ("rps", "log_loss", "brier")}
    half = len(scored) // 2
    result["stack_versus_sharpened_by_half"] = [
        {"events": int(part.stop - part.start), "log_loss": float((table["stack"]["log_loss"][part] - table["sharpened"]["log_loss"][part]).mean()),
         "rps": float((table["stack"]["rps"][part] - table["sharpened"]["rps"][part]).mean())}
        for part in (slice(0, half), slice(half, len(scored)))]
    for key in ("sharpened", "model", "stack"):
        for reference, label in (("market", "versus_normalized_market"), ("raw", "versus_raw_midpoint")):
            result[label][key] = {name: {"difference": float((table[key][name] - table[reference][name]).mean()),
                                         "ci95_by_day": cluster_ci(table[key][name] - table[reference][name], days, rng, draws)}
                                  for name in ("rps", "log_loss", "brier")}
    result["versus_raw_midpoint"]["market"] = {
        name: {"difference": float((table["market"][name] - table["raw"][name]).mean()),
               "ci95_by_day": cluster_ci(table["market"][name] - table["raw"][name], days, rng, draws)}
        for name in ("rps", "log_loss", "brier")}
    # Paper entries from the stack against the raw midpoint plus costs (fills are an assumption).
    result["stack_trading"] = []
    rows = [{**row, "stack": float(events[index]["stack"][position])}
            for index in scored for position, row in enumerate(events[index]["rows"])]
    # Fair values per bucket at the decision time, for the resting-quote replay (weather_maker_backtest.py).
    result["_quotes"] = [
        {"event": row["event"], "date": row["date"], "lead": row["lead"], "id": row["id"], "at": row["at"], "y": row["y"],
         "mid": row["price"], "market": float(events[index]["market"][position]),
         "sharpened": float(events[index]["sharpened"][position]), "model": float(events[index]["model"][position]),
         "stack": float(events[index]["stack"][position])}
        for index in scored for position, row in enumerate(events[index]["rows"])]
    for threshold in (.02, .04, .08):
        pnl, pnl_days, sides = paper_entries(rows, np.asarray([row["stack"] for row in rows]), threshold, half_spread)
        if len(pnl) >= 30:
            values = np.asarray(pnl)
            result["stack_trading"].append({"threshold": threshold, "half_spread": half_spread, "entries": len(values),
                                            "mean_pnl_per_share": float(values.mean()),
                                            "ci95_by_day": cluster_ci(values, np.asarray(pnl_days), rng, draws),
                                            "win_rate": float(np.mean(values > 0)),
                                            "yes_share": float(np.mean(np.asarray(sides) == "yes"))})
    return result


def paper_entries(part: list[dict], probability: np.ndarray, threshold: float, half_spread: float) -> tuple[list, list, list]:
    pnl, days, sides = [], [], []
    for index, row in enumerate(part):
        best = None
        for side, p_side, mid, win in (("yes", probability[index], row["price"], row["y"]),
                                       ("no", 1 - probability[index], 1 - row["price"], 1 - row["y"])):
            cost = mid + half_spread
            cost += row["fee_rate"] * cost * (1 - cost)
            if .03 <= mid <= .97 and p_side - cost >= threshold and (best is None or p_side - cost > best[0]):
                best = (p_side - cost, win - cost, side)
        if best:
            pnl.append(best[1])
            days.append(row["date"])
            sides.append(best[2])
    return pnl, days, sides


def evaluate(directory: Path, weather: Path, fidelity: int, half_spread: float, draws: int,
             window: int, minimum: int, decision_hour: int, complete_only: bool = False,
             dump_quotes: Path | None = None) -> dict:
    events = weather_events(read_rows(directory / "markets.jsonl"))
    if complete_only:                                # every bucket on file, not only the high-volume ones
        complete = {row["slug"] for row in read_rows(directory / "complete-events.jsonl")}
        events = {slug: event for slug, event in events.items() if slug in complete}
    prices = {row["id"]: sorted(row["history"]) for row in read_rows(directory / f"prices-f{fidelity}.jsonl")}
    stations = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in weather.glob("????.json")}
    forecasts = {name: daily_forecasts(station, decision_hour) for name, station in stations.items()}
    observed = {name: {date: value for date, value in station["observed_max_c"].items()
                       if station["observed_count"].get(date, 0) >= 20}       # a partial day understates the maximum
                for name, station in stations.items()}
    report: dict = {"events": len(events), "stations_with_data": len(stations), "decision_hour_local": decision_hour}

    checked = agree = 0                              # station, unit and local-day handling vs official outcomes
    for event in events.values():
        value = observed.get(event["station"], {}).get(event["date"])
        winners = [bucket for bucket in event["buckets"] if bucket["yes"]]
        if value is None or not winners:
            continue
        checked += 1
        agree += winners[0]["low"] <= round(value if event["unit"] == "C" else value * 1.8 + 32) < winners[0]["high"]
    report["metar_max_matches_official_bucket"] = {"events": checked, "share": agree / max(checked, 1)}

    means: dict[int, dict[str, dict]] = {}
    residuals: dict[int, dict[str, Residuals]] = {}
    partial = {name: station.get("observed_partial_max_c") or {} for name, station in stations.items()}
    first_market_day = min(event["date"] for event in events.values())
    report["selection"] = {"validated_before": first_market_day}
    for lead in (1, 2):
        targets = {name: {date: day[f"lead{lead}"] for date, day in forecasts[name].items() if f"lead{lead}" in day}
                   for name in stations}
        config, table = select_config(targets, observed, partial, lead, minimum, first_market_day)
        report["selection"][f"lead{lead}"] = {"chosen": config, "best_five": table[:5],
                                              "default_mae_c": next(row["mae_c"] for row in table
                                                                    if all(row[key] == DEFAULT[key] for key in DEFAULT))}
        means[lead] = {name: walk_forward_means(targets[name], observed[name], lead, minimum, config, partial[name])
                       for name in stations}
        residuals[lead], summary = {}, {}
        for kind in KINDS:
            items = [(date, name, day["spread"], observed[name][date] - day[kind])
                     for name, days in means[lead].items() for date, day in days.items() if date in observed[name]]
            residuals[lead][kind] = Residuals(items)
            errors = np.asarray([item[3] for item in items])
            summary[kind] = {"station_days": len(items), "mae_c": float(np.abs(errors).mean()),
                             "bias_c": float(errors.mean()), "sd_c": float(errors.std())}
        report[f"walk_forward_error_lead{lead}"] = summary

    rows = []
    for event in events.values():
        day = forecasts.get(event["station"], {}).get(event["date"]) or {}
        for lead in (1, 2):
            mean = means[lead].get(event["station"], {}).get(event["date"])
            if mean is None:
                continue
            cutoff = shift_date(event["date"], -lead)
            scales = {kind: residuals[lead][kind].scale(event["station"], cutoff, window, minimum,
                                                        mean["spread"] if kind == "weighted" else None)
                      for kind in KINDS}
            if None in scales.values():
                continue
            decision = day[f"lead{lead}_decision"]
            for bucket in event["buckets"]:
                history = prices.get(bucket["id"])
                if not history:
                    continue
                position = bisect.bisect_right([point[0] for point in history], decision) - 1
                if position < 0 or decision - history[position][0] > 3 * fidelity * 60:
                    continue
                price = history[position][1]
                if not 0 < price < 1:
                    continue
                low, high = ((bucket["low"], bucket["high"]) if event["unit"] == "C"
                             else ((bucket["low"] - 32) / 1.8, (bucket["high"] - 32) / 1.8))
                rows.append({"event": event["slug"], "date": event["date"], "station": event["station"], "lead": lead,
                             "id": bucket["id"], "price": price, "y": float(bucket["yes"]), "fee_rate": bucket["fee_rate"],
                             "low": low, "buckets": len(event["buckets"]), "at": decision,
                             **{kind: bucket_probability(kind, low, high, mean[kind], scales[kind]) for kind in KINDS}})
    rng = np.random.default_rng(20261009)
    quotes: list[dict] = []
    for lead in (1, 2):
        part = [row for row in rows if row["lead"] == lead]
        if len(part) < 100:
            report[f"lead{lead}"] = {"rows": len(part), "skipped": "too few priced buckets"}
            continue
        price, y = np.asarray([row["price"] for row in part]), np.asarray([row["y"] for row in part])
        event_ids = np.asarray([row["event"] for row in part])
        date_ids = np.asarray([row["date"] for row in part])
        signals = {kind: np.asarray([row[kind] for row in part]) for kind in KINDS}
        signals["equal_blend"] = expit((logit(np.clip(price, .001, .999)) + logit(signals["weighted"])) / 2)
        entry = {"rows": len(part), "events": int(len(np.unique(event_ids))), "days": int(len(np.unique(date_ids))),
                 "first_day": min(row["date"] for row in part), "last_day": max(row["date"] for row in part),
                 "brier_market": float(np.mean((price - y) ** 2)), "signals": {}, "trading": []}
        for name, probability in signals.items():
            difference = (probability - y) ** 2 - (price - y) ** 2
            entry["signals"][name] = {"brier": float(np.mean((probability - y) ** 2)),
                                      "minus_market": float(difference.mean()),
                                      "ci95_by_event": cluster_ci(difference, event_ids, rng, draws),
                                      "ci95_by_day": cluster_ci(difference, date_ids, rng, draws)}
        for name in ("weighted", "equal_blend"):
            for threshold in (.02, .05, .10):
                pnl, days, sides = paper_entries(part, signals[name], threshold, half_spread)
                if len(pnl) >= 30:
                    values = np.asarray(pnl)
                    entry["trading"].append({
                        "signal": name, "threshold": threshold, "half_spread": half_spread, "entries": len(values),
                        "mean_pnl_per_share": float(values.mean()),
                        "ci95_by_day": cluster_ci(values, np.asarray(days), rng, draws),
                        "win_rate": float(np.mean(values > 0)), "yes_share": float(np.mean(np.asarray(sides) == "yes")),
                        "days": len(set(days))})
        entry["events_fully_priced"] = event_level(part, rng, draws, half_spread)
        quotes.extend(entry["events_fully_priced"].pop("_quotes", []))
        report[f"lead{lead}"] = entry
    if dump_quotes:
        dump_quotes.parent.mkdir(parents=True, exist_ok=True)
        dump_quotes.write_text("".join(json.dumps(row) + "\n" for row in quotes), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".polycast/slow-market-backfill-v1"))
    parser.add_argument("--weather", type=Path, default=Path(".polycast/slow-market-backfill-v1/weather"))
    parser.add_argument("--fidelity", type=int, default=60)
    parser.add_argument("--half-spread", type=float, default=.01)
    parser.add_argument("--draws", type=int, default=2000)
    parser.add_argument("--window", type=int, default=45, help="trailing station days used for bias, weights and spread")
    parser.add_argument("--minimum-history", type=int, default=10)
    parser.add_argument("--decision-hour", type=int, default=18, help="local hour of the decision, 1-2 days ahead")
    parser.add_argument("--complete-only", action="store_true",
                        help="only events whose every bucket is on file (complete-events.jsonl)")
    parser.add_argument("--dump-quotes", type=Path, help="write per-bucket fair values at the decision time (JSONL)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.directory, args.weather, args.fidelity, args.half_spread, args.draws,
                      args.window, args.minimum_history, args.decision_hour, args.complete_only, args.dump_quotes)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in report if not key.startswith("lead")}))
    for lead in (1, 2):
        entry = report[f"lead{lead}"]
        if "skipped" in entry:
            print(f"lead{lead}: {entry}")
            continue
        print(f"\nlead{lead}: rows={entry['rows']} events={entry['events']} days={entry['days']} "
              f"({entry['first_day']}..{entry['last_day']}) brier market={entry['brier_market']:.5f}")
        for name, item in entry["signals"].items():
            print(f"  {name:11s} brier={item['brier']:.5f} minus market={item['minus_market']:+.5f} "
                  f"ci_event=[{item['ci95_by_event'][0]:+.5f},{item['ci95_by_event'][1]:+.5f}] "
                  f"ci_day=[{item['ci95_by_day'][0]:+.5f},{item['ci95_by_day'][1]:+.5f}]")
        whole = entry["events_fully_priced"]
        if "skipped" not in whole:
            print(f"  whole events: n={whole['events_scored_after_warmup']} of {whole['events']} days={whole['days']} "
                  f"sum(mids)={whole['mean_sum_of_bucket_mids']:.3f} final stack={whole['final_stack_setting']}")
            for key in ("raw", "market", "sharpened", "model", "stack"):
                item = whole["scores"][key]
                print(f"    {key:7s} rps={item['rps']:.4f} log_loss={item['log_loss']:.4f} brier={item['brier']:.5f}")
            for label in ("versus_normalized_market", "versus_raw_midpoint"):
                for key, item in whole[label].items():
                    print(f"    {key:6s} {label:24s} " + " ".join(
                        f"{name}={value['difference']:+.4f}[{value['ci95_by_day'][0]:+.4f},{value['ci95_by_day'][1]:+.4f}]"
                        for name, value in item.items()))
            item = whole["stack_versus_sharpened_market"]
            print("    stack  versus_sharpened_market  " + " ".join(
                f"{name}={value['difference']:+.4f}[{value['ci95_by_day'][0]:+.4f},{value['ci95_by_day'][1]:+.4f}]"
                for name, value in item.items()) + f" | halves {whole['stack_versus_sharpened_by_half']}")
            for trade in whole["stack_trading"]:
                print(f"    stack trade thr={trade['threshold']:.2f} n={trade['entries']:5d} pnl/share={trade['mean_pnl_per_share']:+.4f} "
                      f"ci_day=[{trade['ci95_by_day'][0]:+.4f},{trade['ci95_by_day'][1]:+.4f}] win={trade['win_rate']:.3f} yes={trade['yes_share']:.2f}")
        for trade in entry["trading"]:
            print(f"  trade {trade['signal']:11s} thr={trade['threshold']:.2f} n={trade['entries']:5d} days={trade['days']:3d} "
                  f"pnl/share={trade['mean_pnl_per_share']:+.4f} ci_day=[{trade['ci95_by_day'][0]:+.4f},{trade['ci95_by_day'][1]:+.4f}] "
                  f"win={trade['win_rate']:.3f} yes_share={trade['yes_share']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
