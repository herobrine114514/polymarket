"""Spot-only BTC 5m research features from the public backfill. Offline, numpy only.

Rows are built from Binance 1s closes and the official Gamma outcome; no Polymarket
price enters a row. Public Polymarket price history trails the locally received book by
roughly 15 s and public trade timestamps by a jittery 2-6 s, while this market moves
several cents per second, so the only market benchmark used downstream is the locally
received order book.
Binance is a proxy for the Chainlink TWAP stream; the builder reports how often the
proxy label disagrees with the official one.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path

import numpy as np

RULE_ID = "btc-5m-twap-60"
FEATURES = (
    "elapsed", "remaining", "log_distance_bps", "open_headstart_bps", "twap_distance_bps",
    "sigma_300_bps", "sigma_1800_bps", "sigma_7200_bps",
    "z_300", "z_1800", "z_7200", "theory_logit_1800",
    "ret_5_bps", "ret_15_bps", "ret_60_bps", "ret_300_bps",
    "flow_10", "flow_60", "flow_300", "volume_ratio_60",
    "hour_sin", "hour_cos", "weekend", "previous_proxy_up",
    "locked_fraction", "locked_distance_bps",
)
# Features whose sign flips when Up and Down are exchanged (previous_proxy_up maps to 1-x).
DIRECTIONAL = ("log_distance_bps", "open_headstart_bps", "twap_distance_bps", "z_300", "z_1800", "z_7200",
               "theory_logit_1800", "ret_5_bps", "ret_15_bps", "ret_60_bps", "ret_300_bps",
               "flow_10", "flow_60", "flow_300", "locked_distance_bps")
HISTORY_SECONDS = 7300


def logit(value: float) -> float:
    value = min(1 - 1e-4, max(1e-4, value))
    return math.log(value / (1 - value))


class Spot:
    """Second-indexed spot arrays with prefix sums; `close[s]` is the price at time s+1."""

    def __init__(self, folder: Path):
        chunks = sorted(int(path.stem) for path in folder.glob("*.json"))
        if not chunks:
            raise ValueError("No kline chunks")
        first = chunks[0]
        size = chunks[-1] + 1000 - first
        close = np.full(size, np.nan)
        volume, taker = np.zeros(size), np.zeros(size)
        for chunk in chunks:
            rows = json.loads((folder / f"{chunk}.json").read_text(encoding="utf-8"))
            if not rows:
                continue
            data = np.asarray(rows, dtype=float)
            index = data[:, 0].astype(np.int64) - first
            keep = (index >= 0) & (index < size)
            close[index[keep]], volume[index[keep]], taker[index[keep]] = data[keep, 4], data[keep, 5], data[keep, 6]
        self._index(first, close, volume, taker)

    @classmethod
    def from_arrays(cls, first: int, close, volume=None, taker=None) -> "Spot":
        """Build from in-memory per-second arrays; NaN closes mark seconds without a kline."""
        spot = cls.__new__(cls)
        close = np.asarray(close, dtype=float)
        spot._index(first, close, np.ones(len(close)) if volume is None else np.asarray(volume, dtype=float),
                    np.full(len(close), .5) if taker is None else np.asarray(taker, dtype=float))
        return spot

    def _index(self, first: int, close: np.ndarray, volume: np.ndarray, taker: np.ndarray) -> None:
        self.first, size = first, len(close)
        observed = ~np.isnan(close)
        position = np.where(observed, np.arange(size), 0)
        np.maximum.accumulate(position, out=position)
        self.stale = np.arange(size) - position      # seconds since the last real kline
        self.stale[~observed[position]] = 10 ** 9
        self.close = close[position]
        self.close[self.stale >= 10 ** 9] = 1.0      # placeholder; such rows are rejected by `usable`
        self.log_close = np.log(self.close)
        squared = np.zeros(size)
        squared[5:] = (self.log_close[5:] - self.log_close[:-5]) ** 2
        self._close = np.concatenate(([0.0], np.cumsum(self.close)))
        self._squared = np.concatenate(([0.0], np.cumsum(squared)))
        self._volume = np.concatenate(([0.0], np.cumsum(volume)))
        self._taker = np.concatenate(([0.0], np.cumsum(taker)))
        self._gap = np.concatenate(([0], np.cumsum(self.stale > 30)))

    def usable(self, first_second: int, end_second: int) -> bool:
        a, b = first_second - self.first, end_second - self.first
        return a >= 0 and b <= len(self.close) and self._gap[b] == self._gap[a]

    def mean_close(self, first_second: int, end_second: int) -> float:
        a, b = first_second - self.first, end_second - self.first
        return (self._close[b] - self._close[a]) / (b - a)

    def proxy_up(self, start: int) -> float:
        if not self.usable(start - 60, start + 300):
            return math.nan
        return float(self.mean_close(start + 240, start + 300) >= self.mean_close(start - 60, start))

    def features(self, start: int, decision: int, *, reference: float | None = None,
                 scale: float = 1.0) -> list[float] | None:
        """Inputs end before second `decision`; requires start < decision < start + 300.

        `reference` replaces the proxy opening TWAP with the official one, and `scale`
        (official TWAP / proxy 60s mean) puts proxy prices on the official stream's level.
        """
        if not start < decision < start + 300 or not self.usable(decision - HISTORY_SECONDS, decision):
            return None
        j = decision - 1 - self.first
        elapsed, remaining = decision - start, start + 300 - decision
        if reference is None:
            reference = self.mean_close(start - 60, start)
        spot = self.close[j] * scale
        distance = math.log(spot / reference)
        sigmas = [math.sqrt(max((self._squared[j + 1] - self._squared[j + 1 - lookback]) / lookback / 5, 1e-14))
                  for lookback in (300, 1800, 7200)]
        locked = max(0, 60 - remaining)             # averaging-window seconds already observed
        if remaining >= 60:
            locked_distance = 0.0
            scores = [distance / (sigma * math.sqrt(remaining - 40)) for sigma in sigmas]
        else:
            known = self.mean_close(start + 240, decision) * scale
            locked_distance = math.log(known / reference)
            numerator = locked * known + remaining * spot - 60 * reference
            scores = [numerator / (remaining * spot * sigma * math.sqrt(remaining / 3)) for sigma in sigmas]
        theory = 0.5 * (1 + math.erf(scores[1] / math.sqrt(2)))
        returns = [(self.log_close[j] - self.log_close[j - k]) * 1e4 for k in (5, 15, 60, 300)]
        flows = []
        for k in (10, 60, 300):
            total = self._volume[j + 1] - self._volume[j + 1 - k]
            buys = self._taker[j + 1] - self._taker[j + 1 - k]
            flows.append((2 * buys - total) / total if total > 0 else 0.0)
        recent = self._volume[j + 1] - self._volume[j + 1 - 60]
        baseline = (self._volume[j + 1] - self._volume[j + 1 - 3600]) / 60
        hour = (decision % 86400) / 3600
        weekday = (decision // 86400 + 3) % 7       # Monday=0; 1970-01-01 was a Thursday
        previous = self.proxy_up(start - 300)
        return [elapsed, remaining, distance * 1e4,
                math.log(self.close[start - 1 - self.first] * scale / reference) * 1e4,
                math.log(self.mean_close(decision - 60, decision) * scale / reference) * 1e4,
                sigmas[0] * 1e4, sigmas[1] * 1e4, sigmas[2] * 1e4,
                scores[0], scores[1], scores[2], logit(theory),
                *returns, *flows, recent / baseline if baseline > 0 else 1.0,
                math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24),
                float(weekday >= 5), 0.5 if math.isnan(previous) else previous,
                locked / 60, locked_distance * 1e4]


def read_events(directory: Path) -> dict[int, float]:
    outcomes = {}
    with (directory / "events.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row["rule_id"] == RULE_ID and row["outcome_prices"] in (["1", "0"], ["0", "1"]):
                outcomes[row["start"]] = 1.0 if row["outcome_prices"] == ["1", "0"] else 0.0
    return outcomes


def build_grid(directory: Path, step: int) -> dict:
    outcomes = read_events(directory)
    spot = Spot(directory / "klines-1s-BTCUSDT")
    rows, meta, skipped = [], [], 0
    for start in sorted(outcomes):
        if not spot.usable(start - HISTORY_SECONDS, start + 300):
            skipped += 1
            continue
        proxy = spot.proxy_up(start)
        for decision in range(start + step, start + 300, step):
            rows.append(spot.features(start, decision))
            meta.append([start, decision, outcomes[start], proxy])
    features, meta = np.asarray(rows, dtype=float), np.asarray(meta, dtype=float)
    if not np.all(np.isfinite(features)):
        raise ValueError("Nonfinite feature")
    return {"features": features, "meta": meta, "skipped_windows": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".polycast/btc5m-backfill-v1"))
    parser.add_argument("--step", type=int, default=10)
    args = parser.parse_args()
    result = build_grid(args.directory, args.step)
    meta = result["meta"]
    output = args.directory / f"spot-grid-step{args.step}.npz"
    np.savez_compressed(output, features=result["features"], meta=meta, feature_names=np.asarray(FEATURES))
    per_window = {int(row[0]): (row[2], row[3]) for row in meta}
    print(json.dumps({"output": str(output), "rows": len(meta), "windows": len(per_window),
                      "skipped_windows": result["skipped_windows"],
                      "proxy_label_agreement": round(sum(a == b for a, b in per_window.values()) / len(per_window), 4),
                      "up_share": round(float(np.mean([pair[0] for pair in per_window.values()])), 4)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
