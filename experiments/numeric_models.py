"""Offline, small BTC 5m models. No production imports, orders, or live selection.

Run in oss-numeric-venv. Dataset assembly and chronological partitioning must
validate frozen public snapshots and label availability before calling fit.
"""
from __future__ import annotations
import math
from experiments.causal_features import VERSION as CAUSAL_VERSION, EXTRA_NAMES, extras

FEATURE_NAMES = ("market_logit", "complement_gap", "up_spread", "down_spread",
                 "up_depth_imbalance", "down_depth_imbalance", "coinbase_spread_bps",
                 "coinbase_depth_imbalance", "taker_flow_10s", "taker_flow_30s")
CAUSAL_FEATURE_NAMES = FEATURE_NAMES + EXTRA_NAMES

def feature_names(version: str | None) -> tuple[str, ...]:
    if version == CAUSAL_VERSION: return CAUSAL_FEATURE_NAMES
    if version in (None,'btc5m-public-microstructure-v1','btc5m-public-microstructure-v2-clock-checked'):return FEATURE_NAMES
    raise ValueError('Unknown numerical feature version')
PARAMETERS = {"logistic_l2": .1, "logistic_maxiter": 500, "rounds": 100,
              "tree": {"objective": "binary", "learning_rate": .03, "num_leaves": 7,
                       "max_depth": 3, "min_data_in_leaf": 30, "lambda_l2": 1,
                       "num_threads": 1, "seed": 20261006, "deterministic": True,
                       "force_col_wise": True, "boost_from_average": False, "verbosity": -1}}


def logit(probability: float) -> float:
    value = min(1 - 1e-6, max(1e-6, probability))
    return math.log(value / (1 - value))


def feature_vector(snapshot: dict) -> list[float]:
    """Reject incomplete/future inputs. Trade flow uses receipt-window completeness."""
    now = snapshot["received_at_ms"]
    utc = now
    if snapshot.get("feature_version") in ("btc5m-public-microstructure-v2-clock-checked",CAUSAL_VERSION):
        utc = snapshot.get("as_of_utc_ms")
        clock = snapshot.get("clock_reference") or {}
        if (type(utc) is not int or clock.get("status") != "venues_agree" or
                type(clock.get("checked_at_local_ms")) is not int or
                not 0 <= now - clock["checked_at_local_ms"] <= 120000 or
                isinstance(clock.get("uncertainty_ms"), bool) or not isinstance(clock.get("uncertainty_ms"), (float, int)) or not 0 <= clock["uncertainty_ms"] <= 750 or
                isinstance(clock.get("offset_ms"), bool) or not isinstance(clock.get("offset_ms"), (float, int)) or not math.isfinite(clock["offset_ms"]) or
                abs(clock["offset_ms"]) > 10000 or
                abs(now + clock["offset_ms"] - utc) > 1):
            raise ValueError("UTC clock reference unverified or stale")
    if type(now) is not int or snapshot.get("paired") is not True:
        raise ValueError("Unpaired snapshot")
    market = snapshot["market"]
    if (type(market.get("start_at_ms")) is not int or type(market.get("end_at_ms")) is not int or
            market["end_at_ms"] - market["start_at_ms"] != 300000 or
            not 120000 <= utc - market["start_at_ms"] <= 123000):
        raise ValueError("Outside frozen decision window")
    up, down, cb = snapshot["polymarket"]["up"], snapshot["polymarket"]["down"], snapshot["coinbase"]["book"]
    for book in (up, down, cb):
        receipt, source = book.get("received_at_ms"), book.get("source_timestamp_ms")
        if (type(receipt) is not int or not 0 <= now - receipt <= 5000 or
                source is not None and (type(source) is not int or source > utc) or
                not 0 < book["bid"] < book["ask"] or
                abs(book["spread"] - (book["ask"] - book["bid"])) > 1e-7 or
                abs(book["midpoint"] - (book["bid"] + book["ask"]) / 2) > 1e-7):
            raise ValueError("Stale/future/invalid book")
    if not 0 < up["bid"] < up["ask"] <= 1 or not 0 < down["bid"] < down["ask"] <= 1:
        raise ValueError("Invalid outcome probabilities")
    flows = snapshot["coinbase"]["flow"]
    for window in ("10s", "30s"):
        flow = flows[window]
        if flow.get("complete") is not True or flow.get("taker_imbalance") is None:
            raise ValueError("Incomplete trade window")
    values = [logit(up["midpoint"]), up["midpoint"] + down["midpoint"] - 1,
              up["spread"], down["spread"], up["top5_imbalance"], down["top5_imbalance"],
              cb["spread"] / cb["midpoint"] * 10000, cb["top5_imbalance"],
              flows["10s"]["taker_imbalance"], flows["30s"]["taker_imbalance"]]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        raise ValueError("Nonfinite feature")
    if any(not -1 <= values[index] <= 1 for index in (4, 5, 7, 8, 9)):
        raise ValueError("Invalid imbalance")
    return values + extras(snapshot) if snapshot.get('feature_version') == CAUSAL_VERSION else values


def validate_training(events: list[dict]) -> None:
    if not events or len({row["event_id"] for row in events}) != len(events):
        raise ValueError("Training requires one row per independent event")
    if set(row["outcome"] for row in events) != {0, 1}:
        raise ValueError("Both observed outcomes required")
    if len({row.get("feature_version") for row in events}) != 1:
        raise ValueError("Training cannot mix capture/clock versions")
    names=feature_names(events[0].get('feature_version'))
    for row in events:
        if (len(row["features"]) != len(names) or
                any(not math.isfinite(value) for value in row["features"]) or
                not 0 < row["market_probability"] < 1 or
                abs(row["features"][0] - logit(row["market_probability"])) > 1e-7):
            raise ValueError("Invalid frozen feature/market probability pair")


def fit_logistic(events: list[dict], *, residual: bool) -> dict:
    import numpy as np
    from scipy.optimize import minimize
    from scipy.special import expit
    validate_training(events)
    features = np.asarray([row["features"][1:] if residual else row["features"] for row in events], dtype=float)
    mean, scale = features.mean(axis=0), features.std(axis=0)
    scale[scale < 1e-12] = 1
    x = np.column_stack((np.ones(len(events)), (features - mean) / scale))
    y = np.asarray([row["outcome"] for row in events], dtype=float)
    offset = np.asarray([logit(row["market_probability"]) for row in events]) if residual else np.zeros(len(events))
    penalty = PARAMETERS["logistic_l2"]

    def objective(weights):
        margin = offset + x @ weights
        return (np.mean(np.logaddexp(0, margin) - y * margin) + penalty / 2 * (weights @ weights),
                x.T @ (expit(margin) - y) / len(y) + penalty * weights)

    fit = minimize(objective, np.zeros(x.shape[1]), jac=True, method="L-BFGS-B",
                   options={"maxiter": PARAMETERS["logistic_maxiter"]})
    if not fit.success or not np.all(np.isfinite(fit.x)):
        raise ValueError("Logistic fit did not converge")
    return {"kind": "logistic", "residual": residual, "mean": mean.tolist(), "scale": scale.tolist(),
            "weights": fit.x.tolist(), "objective": float(fit.fun)}


def fit_lightgbm(events: list[dict], *, residual: bool) -> dict:
    import lightgbm as lgb
    import numpy as np
    validate_training(events)
    x = np.asarray([row["features"][1:] if residual else row["features"] for row in events], dtype=float)
    offsets = np.asarray([logit(row["market_probability"]) for row in events]) if residual else np.zeros(len(events))
    dataset = lgb.Dataset(x, label=[row["outcome"] for row in events], init_score=offsets)
    booster = lgb.train(PARAMETERS["tree"], dataset, num_boost_round=PARAMETERS["rounds"])
    return {"kind": "lightgbm", "residual": residual, "model_text": booster.model_to_string()}


def predict(model: dict, events: list[dict]) -> list[float]:
    import numpy as np
    from scipy.special import expit
    features = np.asarray([row["features"][1:] if model["residual"] else row["features"] for row in events], dtype=float)
    if model["kind"] == "logistic":
        x = np.column_stack((np.ones(len(events)), (features - model["mean"]) / model["scale"]))
        margin = x @ np.asarray(model["weights"])
    elif model["kind"] == "lightgbm":
        import lightgbm as lgb
        margin = lgb.Booster(model_str=model["model_text"]).predict(features, raw_score=True, num_threads=1)
    else:
        raise ValueError("Unknown numeric model")
    # LightGBM's init_score is a training offset; it is not included in Booster prediction.
    if model["residual"]:
        margin = margin + np.asarray([logit(row["market_probability"]) for row in events])
    values = expit(margin)
    if not np.all(np.isfinite(values)):
        raise ValueError("Invalid probability output")
    return values.tolist()


def fit_sigmoid(probabilities: list[float], outcomes: list[int]) -> dict:
    """Fit only on an independent chronological calibration partition."""
    import numpy as np
    from scipy.optimize import minimize
    from scipy.special import expit
    if len(probabilities) != len(outcomes) or set(outcomes) != {0, 1} or not all(0 < p < 1 for p in probabilities):
        raise ValueError("Invalid calibration sample")
    x = np.column_stack((np.ones(len(probabilities)), [logit(p) for p in probabilities]))
    y = np.asarray(outcomes)

    def objective(weights):
        z = x @ weights
        return np.mean(np.logaddexp(0, z) - y * z), x.T @ (expit(z) - y) / len(y)

    fitted = minimize(objective, [0., 1.], jac=True, method="L-BFGS-B", bounds=[(-20, 20), (0, 10)])
    if not fitted.success or not np.all(np.isfinite(fitted.x)):
        raise ValueError("Calibration fit failed")
    return {"intercept": float(fitted.x[0]), "slope": float(fitted.x[1])}


def chronological_partitions(events: list[dict], boundaries_ms: tuple[int, int, int], *, embargo_ms=35 * 60000) -> dict:
    """Fixed UTC date boundaries; labels must be observed before the next fit.

    Final holdout rows are reserved and never consumed by the fit helpers.
    No random split, overlapping event, or hindsight label availability.
    """
    if len({row["event_id"] for row in events}) != len(events):
        raise ValueError("Duplicate event labels")
    if (len(boundaries_ms) != 3 or any(type(value) is not int or value % 86400000 for value in boundaries_ms) or
            not boundaries_ms[0] < boundaries_ms[1] < boundaries_ms[2]):
        raise ValueError("Require three increasing UTC date boundaries")
    partitions = {"train": [], "calibration": [], "validation": [], "holdout": [], "excluded": []}
    for row in sorted(events, key=lambda item: (item["as_of_ms"], item["event_id"])):
        decision, end, observed = row["as_of_ms"], row["end_at_ms"], row["label_observed_at_ms"]
        if (not all(type(value) is int for value in (decision, end, observed)) or not decision < end <= observed):
            raise ValueError("Invalid event/label clocks")
        index = sum(decision >= boundary for boundary in boundaries_ms)
        name = ("train", "calibration", "validation", "holdout")[index]
        if index < 3 and (end + embargo_ms >= boundaries_ms[index] or observed >= boundaries_ms[index]):
            partitions["excluded"].append({"event_id": row["event_id"], "reason": "overlap_or_label_not_available"})
        else:
            partitions[name].append(row)
    return partitions
