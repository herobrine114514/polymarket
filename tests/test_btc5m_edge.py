from __future__ import annotations
import gzip
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path

from experiments.btc5m_backfill import archive_snapshots, event_row

NUMERIC = all(importlib.util.find_spec(name) for name in ("numpy", "scipy", "lightgbm"))
START = 1_791_000_000  # multiple of 300


class BackfillParsingTests(unittest.TestCase):
    def test_event_row_keeps_rule_and_official_outcome(self):
        event = {"slug": f"btc-updown-5m-{START}", "markets": [{
            "id": 7, "conditionId": "0xabc", "clobTokenIds": '["11", "22"]', "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0", "1"]', "cryptoMarketConfig": {"id": "btc-5m-twap-60"}, "closed": True,
            "feeSchedule": {"rate": 0.07, "exponent": 1}}]}
        row = event_row(event)
        self.assertEqual((row["start"], row["up_token"], row["outcome_prices"], row["rule_id"], row["fee_rate"]),
                         (START, "11", ["0", "1"], "btc-5m-twap-60", 0.07))
        self.assertIsNone(event_row({"slug": "eth-updown-5m-1", "markets": event["markets"]}))
        self.assertIsNone(event_row({"slug": event["slug"], "markets": []}))

    def test_archive_snapshots_reads_only_feature_snapshots(self):
        snapshot = {"kind": "feature_snapshot", "market": {"start_at_ms": START * 1000},
                    "as_of_utc_ms": START * 1000 + 61_500, "received_at_ms": START * 1000 + 63_000,
                    "clock_reference": {"offset_ms": -1500},
                    "polymarket": {"up": {"bid": .41, "ask": .42, "source_timestamp_ms": START * 1000 + 61_000},
                                   "down": {"bid": .58, "ask": .59}, "up_quality": {"valid": True},
                                   "down_quality": {"valid": True}},
                    "coinbase": {"book": {"midpoint": 82000.5}}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw-x-00000.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps({"kind": "raw_frame", "raw_text": '"feature_snapshot"'}) + "\n")
                handle.write(json.dumps(snapshot) + "\n")
            rows = archive_snapshots(str(path))
            self.assertEqual(archive_snapshots(str(Path(folder) / "missing.jsonl.gz")), [])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["start"], rows[0]["up_ask"], rows[0]["down_bid"], rows[0]["recv_ms"], rows[0]["cb_mid"]),
                         (START, .42, .58, START * 1000 + 63_000, 82000.5))


@unittest.skipUnless(NUMERIC, "isolated numeric environment required")
class SpotFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        from experiments.btc5m_edge_dataset import FEATURES, HISTORY_SECONDS, Spot
        cls.np, cls.names, cls.Spot = np, list(FEATURES), Spot
        cls.windows = 1500
        rng = np.random.default_rng(11)
        cls.first = START - HISTORY_SECONDS - 300
        size = HISTORY_SECONDS + 300 + cls.windows * 300
        cls.close = 80000 * np.exp(np.cumsum(rng.normal(0, 1.2e-4, size)))
        cls.spot = Spot.from_arrays(cls.first, cls.close)

    def column(self, rows, name):
        return self.np.asarray([row[self.names.index(name)] for row in rows])

    def test_closed_form_twap_probability_is_calibrated_on_simulated_paths(self):
        np = self.np
        rows, labels = [], []
        for window in range(self.windows):
            start = START + window * 300
            for offset in (30, 150, 235, 270, 290):
                rows.append(self.spot.features(start, start + offset))
                labels.append(self.spot.proxy_up(start))
        y = np.asarray(labels)
        theory = 1 / (1 + np.exp(-self.column(rows, "theory_logit_1800")))
        self.assertLess(np.mean((theory - y) ** 2), .15)
        for low, high in ((0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)):
            mask = (theory >= low) & (theory < high)
            self.assertGreater(mask.sum(), 300)
            self.assertLess(abs(theory[mask].mean() - y[mask].mean()), .04)
        late = self.column(rows, "remaining") == 10
        self.assertGreater(np.mean((theory[late] > .5) == (y[late] == 1)), .97)

    def test_features_ignore_spot_at_or_after_the_decision_second(self):
        start, decision = START + 300 * 7, START + 300 * 7 + 251
        changed = self.close.copy()
        changed[decision - self.first:] *= 1.05      # close[s] is the price at s+1: index `decision` is the future
        self.assertEqual(self.spot.features(start, decision),
                         self.Spot.from_arrays(self.first, changed).features(start, decision))
        changed = self.close.copy()
        changed[decision - 1 - self.first] *= 1.05   # the last allowed second must matter
        self.assertNotEqual(self.spot.features(start, decision),
                            self.Spot.from_arrays(self.first, changed).features(start, decision))

    def test_official_reference_and_scale_are_level_invariant(self):
        start, decision = START + 300 * 3, START + 300 * 3 + 262
        base = self.spot.features(start, decision)
        reference = self.spot.mean_close(start - 60, start)
        self.assertEqual(base, self.spot.features(start, decision, reference=reference, scale=1.0))
        scaled = self.spot.features(start, decision, reference=reference * 1.0007, scale=1.0007)
        for a, b in zip(base, scaled):
            self.assertAlmostEqual(a, b, places=6)
        higher = self.spot.features(start, decision, reference=reference * 1.001, scale=1.0)
        index = self.names.index("log_distance_bps")
        self.assertAlmostEqual(base[index] - higher[index], math.log(1.001) * 1e4, places=6)

    def test_gaps_and_out_of_window_decisions_are_rejected(self):
        start = START + 300 * 5
        self.assertIsNone(self.spot.features(start, start))
        self.assertIsNone(self.spot.features(start, start + 300))
        broken = self.close.copy()
        broken[start - 2000 - self.first:start - 1900 - self.first] = self.np.nan
        gapped = self.Spot.from_arrays(self.first, broken)
        self.assertIsNone(gapped.features(start, start + 100))
        self.assertTrue(math.isnan(gapped.proxy_up(start - 2100)))


@unittest.skipUnless(NUMERIC, "isolated numeric environment required")
class EdgeModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        from experiments import btc5m_edge_model as model
        from experiments.btc5m_edge_dataset import HISTORY_SECONDS, Spot
        cls.np, cls.model = np, model
        rng = np.random.default_rng(5)
        first = START - HISTORY_SECONDS - 300
        cls.spot = Spot.from_arrays(first, 80000 * np.exp(np.cumsum(rng.normal(0, 1.2e-4, HISTORY_SECONDS + 300 + 600 * 300))))

    def test_mirrored_inputs_give_complementary_probabilities(self):
        np, model = self.np, self.model
        rows, labels = [], []
        for window in range(600):
            start = START + window * 300
            for offset in range(20, 300, 40):
                rows.append(self.spot.features(start, start + offset))
                labels.append(self.spot.proxy_up(start))
        x = model.prepared(np.asarray(rows))
        np.testing.assert_allclose(model.flipped(model.flipped(x)), x)
        output = model.predict(model.fit_models(x, np.asarray(labels)), x)
        mirrored = model.predict(model.fit_models(x, np.asarray(labels)), model.flipped(x))
        for key in ("theory", "linear", "tree"):
            np.testing.assert_allclose(output[key] + mirrored[key], 1, atol=1e-9)
        self.assertLess(np.mean((output["tree"] - np.asarray(labels)) ** 2), .16)

    def test_official_reference_uses_only_ticks_already_received(self):
        model = self.model
        start = START + 300 * 9
        proxy = self.spot.mean_close(start - 60, start)
        ticks = {start: (proxy * 1.001, 1_000), start + 5: (1.0, 9_000), start + 4: (self.spot.mean_close(start - 56, start + 4), 2_000)}
        reference = model.OfficialReference(ticks, self.spot, start)
        self.assertIsNone(reference.at(999, start + 6))                 # opening tick not received yet
        opening, scale = reference.at(2_500, start + 6)                 # the start+5 tick arrives later
        self.assertEqual(opening, proxy * 1.001)
        self.assertAlmostEqual(scale, 1.0, places=9)
        self.assertIsNone(reference.at(2_500, start + 40))              # newest received tick is too old
        self.assertIsNone(model.OfficialReference({}, self.spot, start).at(10 ** 15, start + 6))

    def test_delayed_fok_fills_only_at_or_below_the_frozen_ask(self):
        model = self.model

        def row(start, elapsed, later_up_ask, y=1.0, probability=.7):
            return {"start": start, "elapsed": elapsed, "y": y, "p": probability, "up_ask": .5, "down_ask": .51,
                    "later": {1: None if later_up_ask is None else {"up_ask": later_up_ask, "down_ask": .9}}}

        rows = [row(1, 10, .52), row(1, 11, .49), row(1, 12, .40),      # killed, filled at .49, then window is done
                row(2, 10, None), row(3, 256, .5), row(4, 10, .5, y=0.0)]
        result = model.fok_replay(rows, .02, 1, "p")
        self.assertEqual((result["attempts"], result["fills"]), (4, 2))
        win = 1 - .49 - .07 * .49 * .51
        loss = 0 - .5 - .07 * .5 * .5
        self.assertAlmostEqual(result["total_pnl_shares"], win + loss, places=12)
        self.assertEqual(model.fok_replay(rows, .5, 1, "p")["fills"], 0)

    def test_local_book_loader_drops_stale_or_invalid_tops_and_keeps_the_last_per_second(self):
        base = {"start": START, "recv_ms": 1, "up_bid": .40, "up_ask": .41, "down_bid": .59, "down_ask": .60,
                "up_valid": True, "down_valid": True}
        at = START * 1000 + 30_000
        rows = [{**base, "as_of_ms": at + 100, "up_src_ms": at},
                {**base, "as_of_ms": at + 900, "up_src_ms": at + 800, "up_bid": .43, "up_ask": .44},
                {**base, "as_of_ms": at + 1_500, "up_src_ms": at - 4_000},                 # book older than 3 s
                {**base, "as_of_ms": at + 2_500, "up_src_ms": at + 2_400, "up_valid": False},
                {**base, "as_of_ms": at + 3_500, "up_src_ms": at + 3_400, "up_bid": .45, "up_ask": .45},
                {**base, "start": START + 300, "as_of_ms": at + 300_000, "up_src_ms": at + 300_000}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            book = self.model.load_book(path, {START: 1.0}, 3000)
        self.assertEqual(list(book), [START])
        self.assertEqual(list(book[START]), [START + 30])
        self.assertEqual(book[START][START + 30]["up_ask"], .44)


if __name__ == "__main__":
    unittest.main()
