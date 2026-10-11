"""Can a numeric BTC 5m model match or beat the Polymarket book? Offline research only.

Stage `outcomes`: chronological folds on the spot-only grid, scored against official
outcomes (closed-form TWAP diffusion vs fitted models). No market price involved.
Stage `book`: a model fitted only on windows that ended before the first locally
recorded window is compared with the locally received Polymarket midpoint, second by
second, at several spot-feed lags. `proxy_*` uses Binance alone; `official_*` uses the
relayed Chainlink opening TWAP and price level as they had been received locally.
Then a blocked-CV blend and a delayed-FOK paper replay against the recorded asks.
Costs use the public taker fee rate*p*(1-p). Run in .polycast/oss-numeric-venv.
Nothing here places or routes orders.
"""
from __future__ import annotations
import argparse
import bisect
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit as np_logit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.btc5m_edge_dataset import DIRECTIONAL, FEATURES, Spot, read_events

CLIP = {"z_300": 8, "z_1800": 8, "z_7200": 8, "ret_5_bps": 50, "ret_15_bps": 80, "ret_60_bps": 150,
        "ret_300_bps": 300, "log_distance_bps": 300, "open_headstart_bps": 100, "twap_distance_bps": 300,
        "locked_distance_bps": 300, "volume_ratio_60": 20, "theory_logit_1800": 9.3}
TREE = {"objective": "binary", "learning_rate": .05, "num_leaves": 31, "max_depth": 6,
        "min_data_in_leaf": 500, "feature_fraction": .8, "bagging_fraction": .8, "bagging_freq": 1,
        "lambda_l2": 10, "num_threads": 6, "seed": 20261008, "deterministic": True,
        "force_col_wise": True, "boost_from_average": False, "verbosity": -1}
ROUNDS, EMBARGO, FEE_RATE = 200, 900, .07
NAMES = list(FEATURES)
SIGN = np.asarray([-1.0 if name in DIRECTIONAL else 1.0 for name in NAMES])
PREVIOUS = NAMES.index("previous_proxy_up")
THEORY = NAMES.index("theory_logit_1800")
LINEAR = [NAMES.index(name) for name in ("z_300", "z_1800", "z_7200")]
MODEL_KEYS = ("proxy_theory", "proxy_tree", "official_theory", "official_tree")


def prepared(features: np.ndarray) -> np.ndarray:
    x = np.array(features, dtype=float)
    for name, limit in CLIP.items():
        column = NAMES.index(name)
        x[:, column] = np.clip(x[:, column], -limit, limit)
    return x


def flipped(x: np.ndarray) -> np.ndarray:
    """Exchange Up and Down: the rule is symmetric apart from exact ties."""
    mirror = x * SIGN
    mirror[:, PREVIOUS] = 1 - x[:, PREVIOUS]
    return mirror


def fit_models(x: np.ndarray, y: np.ndarray) -> dict:
    import lightgbm as lgb
    both_x, both_y = np.vstack((x, flipped(x))), np.concatenate((y, 1 - y))
    booster = lgb.train(TREE, lgb.Dataset(both_x, label=both_y, init_score=both_x[:, THEORY]), num_boost_round=ROUNDS)
    design = both_x[:, LINEAR]

    def objective(weights):
        margin = design @ weights
        return np.mean(np.logaddexp(0, margin) - both_y * margin), design.T @ (expit(margin) - both_y) / len(both_y)

    linear = minimize(objective, np.zeros(len(LINEAR)), jac=True, method="L-BFGS-B").x
    return {"booster": booster, "linear": linear}


def predict(models: dict, x: np.ndarray) -> dict[str, np.ndarray]:
    def tree_margin(values):
        return values[:, THEORY] + models["booster"].predict(values, raw_score=True, num_threads=6)
    # Average the direct and mirrored margins so p(x) + p(flip(x)) = 1 exactly.
    return {"theory": expit(x[:, THEORY]), "linear": expit(x[:, LINEAR] @ models["linear"]),
            "tree": expit((tree_margin(x) - tree_margin(flipped(x))) / 2)}


def scores(probability: np.ndarray, y: np.ndarray) -> dict:
    clipped = np.clip(probability, 1e-4, 1 - 1e-4)
    return {"brier": float(np.mean((probability - y) ** 2)),
            "log_loss": float(-np.mean(y * np.log(clipped) + (1 - y) * np.log(1 - clipped)))}


def clustered_difference(loss_a: np.ndarray, loss_b: np.ndarray, clusters: np.ndarray,
                         rng: np.random.Generator, draws: int = 4000) -> dict:
    """Mean of (loss_a - loss_b) with a bootstrap that resamples whole clusters."""
    _, inverse = np.unique(clusters, return_inverse=True)
    count = np.bincount(inverse).astype(float)
    total = np.bincount(inverse, weights=loss_a - loss_b)
    picks = rng.integers(0, len(count), size=(draws, len(count)))
    samples = total[picks].sum(axis=1) / count[picks].sum(axis=1)
    low, high = np.percentile(samples, [2.5, 97.5])
    return {"difference": float(total.sum() / count.sum()), "ci95": [float(low), float(high)],
            "share_of_draws_a_better": float(np.mean(samples < 0)), "clusters": int(len(count))}


def load_grid(path: Path) -> tuple[np.ndarray, np.ndarray]:
    data = np.load(path, allow_pickle=False)
    if [str(name) for name in data["feature_names"]] != NAMES:
        raise ValueError("Grid was built with a different feature list")
    return prepared(data["features"]), data["meta"]


def outcome_folds(grid: Path, boundaries: list[int]) -> dict:
    """Fit before each boundary, score the following segment against official outcomes."""
    x, meta = load_grid(grid)
    starts, decision, y = meta[:, 0].astype(np.int64), meta[:, 1].astype(np.int64), meta[:, 2]
    rng = np.random.default_rng(20261008)
    report = {"grid": str(grid), "rows": int(len(y)), "windows": int(len(np.unique(starts))), "folds": []}
    edges = boundaries + [int(starts.max()) + 300]
    for boundary, end in zip(edges[:-1], edges[1:]):
        train, test = starts + 300 + EMBARGO <= boundary, (starts >= boundary) & (starts < end)
        if train.sum() < 50000 or not test.any():
            continue
        output = predict(fit_models(x[train], y[train]), x[test])
        elapsed = decision[test] - starts[test]
        fold = {"train_before": boundary, "test_until": end, "train_windows": int(len(np.unique(starts[train]))),
                "test_windows": int(len(np.unique(starts[test]))), "segments": {}}
        segments = {"all": np.ones(test.sum(), bool)}
        for low, high in ((0, 60), (60, 120), (120, 180), (180, 240), (240, 300)):
            segments[f"elapsed_{low}_{high}"] = (elapsed >= low) & (elapsed < high)
        for label, mask in segments.items():
            entry = {key: scores(value[mask], y[test][mask]) for key, value in output.items()}
            entry["tree_vs_theory"] = clustered_difference(
                (output["tree"][mask] - y[test][mask]) ** 2, (output["theory"][mask] - y[test][mask]) ** 2,
                starts[test][mask], rng)
            fold["segments"][label] = entry
        report["folds"].append(fold)
    return report


def load_book(path: Path, outcomes: dict[int, float], max_age_ms: int) -> dict[int, dict[int, dict]]:
    """Last valid locally received top of book per (window, UTC second)."""
    book: dict[int, dict[int, dict]] = defaultdict(dict)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            start = row["start"]
            if (start not in outcomes or not row.get("up_valid") or not row.get("down_valid") or
                    None in (row.get("as_of_ms"), row.get("recv_ms"), row.get("up_bid"), row.get("up_ask"),
                             row.get("down_bid"), row.get("down_ask"), row.get("up_src_ms"))):
                continue
            second = row["as_of_ms"] // 1000
            if (not 0 < row["up_bid"] < row["up_ask"] < 1 or not 0 < row["down_bid"] < row["down_ask"] < 1 or
                    not start < second < start + 300 or row["as_of_ms"] - row["up_src_ms"] > max_age_ms):
                continue
            previous = book[start].get(second)
            if previous is None or row["as_of_ms"] >= previous["as_of_ms"]:
                book[start][second] = row
    return book


def load_ticks(path: Path) -> dict[int, tuple[float, int]]:
    """Chainlink TWAP-60 relay ticks: UTC second -> (value, earliest local receipt ms)."""
    ticks: dict[int, tuple[float, int]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row["timestamp_ms"] % 1000:
                continue
            second, value = row["timestamp_ms"] // 1000, int(row["full_accuracy_value"]) / 1e18
            if second not in ticks or row["received_at_ms"] < ticks[second][1]:
                ticks[second] = (value, row["received_at_ms"])
    return ticks


class OfficialReference:
    """Official opening TWAP and proxy-to-official price scale, as known at a local receipt time."""

    def __init__(self, ticks: dict[int, tuple[float, int]], spot: Spot, start: int):
        self.opening, self.spot, self.ticks = ticks.get(start), spot, ticks
        received = sorted((ticks[second][1], second) for second in range(start - 60, start + 300) if second in ticks)
        self.receipts = [item[0] for item in received]
        self.latest, newest = [], -1
        for _, second in received:
            newest = max(newest, second)
            self.latest.append(newest)

    def at(self, local_receipt_ms: int, utc_second: int) -> tuple[float, float] | None:
        if self.opening is None or self.opening[1] > local_receipt_ms:
            return None
        position = bisect.bisect_right(self.receipts, local_receipt_ms) - 1
        if position < 0:
            return None
        tick_second = self.latest[position]
        if utc_second - tick_second > 20 or not self.spot.usable(tick_second - 60, tick_second):
            return None
        return self.opening[0], self.ticks[tick_second][0] / self.spot.mean_close(tick_second - 60, tick_second)


def fok_replay(rows: list[dict], threshold: float, delay: int, probability_key: str) -> dict:
    """One paper entry per window: freeze the ask, wait `delay` seconds, fill only at or below it."""
    pnl, attempts, filled_windows = [], 0, set()
    for row in rows:
        if row["start"] in filled_windows or not 5 <= row["elapsed"] <= 255:
            continue
        best = None
        for side, p_side, ask, win in (("up", row[probability_key], row["up_ask"], row["y"]),
                                       ("down", 1 - row[probability_key], row["down_ask"], 1 - row["y"])):
            if not .05 <= ask <= .95:
                continue
            edge = p_side - ask - FEE_RATE * ask * (1 - ask)
            if edge >= threshold and (best is None or edge > best[0]):
                best = (edge, side, ask, win)
        if best is None:
            continue
        attempts += 1
        later = row["later"].get(delay)
        if later is None:
            continue
        fill = later["up_ask"] if best[1] == "up" else later["down_ask"]
        if fill > best[2] + 1e-9:
            continue                                  # price moved away: FOK killed
        filled_windows.add(row["start"])
        pnl.append(best[3] - fill - FEE_RATE * fill * (1 - fill))
    if not pnl:
        return {"threshold": threshold, "delay": delay, "attempts": attempts, "fills": 0}
    values = np.asarray(pnl)
    return {"threshold": threshold, "delay": delay, "attempts": attempts, "fills": len(values),
            "mean_pnl_per_share": float(values.mean()), "total_pnl_shares": float(values.sum()),
            "t_stat": float(values.mean() / (values.std(ddof=1) / np.sqrt(len(values)))) if len(values) > 2 else None,
            "win_rate": float(np.mean(values > 0))}


def book_comparison(directory: Path, grid: Path, lags: list[int], ticks_path: Path, max_age_ms: int) -> dict:
    outcomes = read_events(directory)
    book = load_book(directory / "local-book-snapshots.jsonl", outcomes, max_age_ms)
    spot = Spot(directory / "klines-1s-BTCUSDT")
    ticks = load_ticks(ticks_path)
    first_local = min(book)
    x, meta = load_grid(grid)
    train = meta[:, 0] + 300 + EMBARGO <= first_local
    models = fit_models(x[train], meta[train, 2])
    rng = np.random.default_rng(20261008)
    report = {"first_local_window": int(first_local), "train_windows": int(len(np.unique(meta[train, 0]))),
              "local_windows": len(book), "max_book_age_ms": max_age_ms, "lags": {}, "blend": {}, "replay": {}}
    gain = models["booster"].feature_importance(importance_type="gain")
    report["tree_gain_share"] = {name: round(float(share), 4) for name, share in
                                 sorted(zip(NAMES, gain / gain.sum()), key=lambda item: -item[1])[:12]}
    references = {start: OfficialReference(ticks, spot, start) for start in book}
    per_lag = {}
    for lag in lags:
        keys, proxy_features, official_features = [], [], []
        for start in sorted(book):
            for second in sorted(book[start]):
                context = references[start].at(book[start][second]["recv_ms"], second)
                proxy = spot.features(start, second - lag)
                if context is None or proxy is None:
                    continue
                keys.append((start, second))
                proxy_features.append(proxy)
                official_features.append(spot.features(start, second - lag, reference=context[0], scale=context[1]))
        proxy_output = predict(models, prepared(np.asarray(proxy_features)))
        official_output = predict(models, prepared(np.asarray(official_features)))
        output = {"proxy_theory": proxy_output["theory"], "proxy_tree": proxy_output["tree"],
                  "official_theory": official_output["theory"], "official_tree": official_output["tree"]}
        rows = []
        for index, (start, second) in enumerate(keys):
            top = book[start][second]
            rows.append({"start": start, "second": second, "elapsed": second - start, "y": outcomes[start],
                         "mid": (top["up_bid"] + top["up_ask"]) / 2, "up_ask": top["up_ask"], "down_ask": top["down_ask"],
                         **{key: float(value[index]) for key, value in output.items()},
                         "later": {delay: book[start].get(second + delay) for delay in (1, 2, 3)}})
        per_lag[lag] = rows
        y = np.asarray([row["y"] for row in rows])
        mid = np.asarray([row["mid"] for row in rows])
        clusters = np.asarray([row["start"] for row in rows])
        elapsed = np.asarray([row["elapsed"] for row in rows])
        entry = {"rows": len(rows), "windows": int(len(np.unique(clusters))), "segments": {}}
        segments = {"all": np.ones(len(rows), bool), "active_mid_05_95": (mid >= .05) & (mid <= .95),
                    "elapsed_5_240": (elapsed >= 5) & (elapsed < 240), "elapsed_240_300": elapsed >= 240}
        for label, mask in segments.items():
            part = {"rows": int(mask.sum()), "market": scores(mid[mask], y[mask])}
            for key in MODEL_KEYS:
                probability = np.asarray([row[key] for row in rows])[mask]
                part[key] = scores(probability, y[mask])
                part[key]["brier_vs_market"] = clustered_difference(
                    (probability - y[mask]) ** 2, (mid[mask] - y[mask]) ** 2, clusters[mask], rng)
            entry["segments"][label] = part
        report["lags"][str(lag)] = entry
    # Blend: sigmoid(a + b*logit(mid) + c*logit(model)), fitted on four time blocks, scored on the fifth.
    for lag in lags:
        rows = per_lag[lag]
        active = [row for row in rows if .02 <= row["mid"] <= .98]
        y = np.asarray([row["y"] for row in active])
        clusters = np.asarray([row["start"] for row in active])
        design = np.column_stack((np.ones(len(active)), np_logit([row["mid"] for row in active]),
                                  np_logit(np.clip([row["official_tree"] for row in active], 1e-4, 1 - 1e-4))))
        windows = np.unique(clusters)
        block = np.searchsorted(windows, clusters) * 5 // len(windows)
        blended, weights = np.full(len(active), np.nan), []
        for held in range(5):
            fit_rows, test_rows = block != held, block == held

            def objective(w):
                margin = design[fit_rows] @ w
                return (np.mean(np.logaddexp(0, margin) - y[fit_rows] * margin),
                        design[fit_rows].T @ (expit(margin) - y[fit_rows]) / fit_rows.sum())

            fitted = minimize(objective, np.asarray([0., 1., 0.]), jac=True, method="L-BFGS-B").x
            weights.append([round(float(value), 4) for value in fitted])
            blended[test_rows] = expit(design[test_rows] @ fitted)
        mid = np.asarray([row["mid"] for row in active])
        # Fixed equal-weight log-odds average: no fitted parameter, so no fitting risk.
        average = expit((design[:, 1] + design[:, 2]) / 2)
        report["blend"][str(lag)] = {
            "rows": len(active), "weights_intercept_mid_model": weights, "market": scores(mid, y),
            "blend": scores(blended, y), "equal_average": scores(average, y),
            "blend_vs_market": clustered_difference((blended - y) ** 2, (mid - y) ** 2, clusters, rng),
            "equal_average_vs_market": clustered_difference((average - y) ** 2, (mid - y) ** 2, clusters, rng)}
        report["replay"][str(lag)] = [fok_replay(rows, threshold, delay, "official_tree")
                                      for delay in (1, 2, 3) for threshold in (0., .02, .04)]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("outcomes", "book"))
    parser.add_argument("--directory", type=Path, default=Path(".polycast/btc5m-backfill-v1"))
    parser.add_argument("--grid", type=Path, default=Path(".polycast/btc5m-backfill-v1/spot-grid-step10.npz"))
    parser.add_argument("--ticks", type=Path, default=Path(".polycast/chainlink_twap60.jsonl"))
    parser.add_argument("--boundaries", type=int, nargs="*", default=[])
    parser.add_argument("--lags", type=int, nargs="*", default=[0, 1, 2, 3, 5])
    parser.add_argument("--max-book-age-ms", type=int, default=3000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = (outcome_folds(args.grid, args.boundaries) if args.stage == "outcomes"
              else book_comparison(args.directory, args.grid, args.lags, args.ticks, args.max_book_age_ms))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.stage == "outcomes":
        for fold in report["folds"]:
            print(f"\ntrain_windows={fold['train_windows']} test_windows={fold['test_windows']}")
            for label, entry in fold["segments"].items():
                difference = entry["tree_vs_theory"]
                print(f"  {label:16s} theory={entry['theory']['brier']:.5f} linear={entry['linear']['brier']:.5f} "
                      f"tree={entry['tree']['brier']:.5f} tree-theory={difference['difference']:+.5f} "
                      f"ci=[{difference['ci95'][0]:+.5f},{difference['ci95'][1]:+.5f}]")
        return 0
    print(f"train_windows={report['train_windows']} local_windows={report['local_windows']}")
    for lag, entry in report["lags"].items():
        print(f"\nlag={lag}s rows={entry['rows']} windows={entry['windows']}")
        for label, part in entry["segments"].items():
            line = f"  {label:17s} n={part['rows']:6d} market={part['market']['brier']:.5f}"
            for key in ("proxy_tree", "official_theory", "official_tree"):
                difference = part[key]["brier_vs_market"]
                line += (f" | {key}={part[key]['brier']:.5f} d={difference['difference']:+.5f} "
                         f"[{difference['ci95'][0]:+.4f},{difference['ci95'][1]:+.4f}]")
            print(line)
    print("\nblend with official_tree vs market (rows with mid in [0.02, 0.98])")
    for lag, entry in report["blend"].items():
        fitted, equal = entry["blend_vs_market"], entry["equal_average_vs_market"]
        print(f"  lag={lag}s market={entry['market']['brier']:.5f} cv_blend d={fitted['difference']:+.5f} "
              f"[{fitted['ci95'][0]:+.5f},{fitted['ci95'][1]:+.5f}] equal_average d={equal['difference']:+.5f} "
              f"[{equal['ci95'][0]:+.5f},{equal['ci95'][1]:+.5f}] weights={entry['weights_intercept_mid_model'][0]}")
    print("\ndelayed-FOK paper replay of official_tree against recorded asks (pnl per share after fee)")
    for lag, rows in report["replay"].items():
        for row in rows:
            if row["fills"]:
                print(f"  lag={lag}s thr={row['threshold']:.2f} delay={row['delay']}s attempts={row['attempts']:5d} "
                      f"fills={row['fills']:4d} mean={row['mean_pnl_per_share']:+.4f} t={row['t_stat'] or 0:+.2f} "
                      f"win={row['win_rate']:.3f}")
    print("\ntop gain:", report["tree_gain_share"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
