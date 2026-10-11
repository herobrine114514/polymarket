from __future__ import annotations
import importlib.util
import unittest

NUMERIC = importlib.util.find_spec("numpy") is not None
T0 = 1_791_331_200  # 2026-10-07T00:00:00Z


@unittest.skipUnless(NUMERIC, "isolated numeric environment required")
class FlowFollowingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from experiments import weather_flow_following as flow
        from experiments import weather_settlement_edge as settle
        cls.flow, cls.settle = flow, settle

    def test_trade_result_scores_buys_and_sells_of_either_token(self):
        result = self.flow.trade_result
        self.assertEqual(result([T0, 0, 1, .30, 10, "a"], True), (.70, .30))            # bought Yes, it won
        self.assertEqual(result([T0, 0, 0, .30, 10, "a"], True), (-.70, .30))           # sold Yes that won
        self.assertAlmostEqual(result([T0, 1, 1, .80, 10, "a"], True)[0], -.80)         # bought No, Yes won
        self.assertAlmostEqual(result([T0, 1, 0, .80, 10, "a"], False)[0], -.20)        # sold No that paid out

    def test_rating_uses_only_events_at_least_two_days_old(self):
        flow = self.flow
        per_event = {"w": {f"2026-09-{day:02d}": [3.0, 1.0] for day in range(1, 11)}}
        per_event["w"]["2026-09-11"] = [-900.0, 1.0]                                    # a disaster two days from now
        ratings = flow.Ratings(flow.event_scores(per_event))
        self.assertEqual(ratings.as_of("w", "2026-09-12")[0], 10)                       # 09-11 is not resolved knowledge yet
        self.assertAlmostEqual(ratings.as_of("w", "2026-09-12")[1], 30 / 10 ** .5)
        self.assertEqual(ratings.kind("w", "2026-09-12", 8), "sharp")
        self.assertEqual(ratings.kind("w", "2026-09-13", 8), "dull")                    # now it is
        self.assertIsNone(ratings.kind("w", "2026-09-05", 8))                           # only three events known
        self.assertIsNone(ratings.kind("stranger", "2026-09-12", 8))

    def test_follower_pays_the_next_real_buy_price_once_per_token(self):
        flow = self.flow
        per_event = {"lead": {f"2026-09-{day:02d}": [3.0, 1.0] for day in range(1, 11)}}
        ratings = flow.Ratings(flow.event_scores(per_event))
        when = T0                                                                        # 2026-10-07, long after those events
        tape = [[when, 0, 1, .40, 20, "lead"], [when + 30, 0, 1, .41, 20, "x"],          # inside the delay: not an entry
                [when + 90, 1, 1, .58, 20, "x"],                                         # other token
                [when + 120, 0, 1, .45, 20, "x"],                                        # the follower's price
                [when + 500, 0, 1, .40, 20, "lead"], [when + 700, 0, 1, .60, 20, "x"]]   # second signal on the same token: ignored
        events = {"e": {"slug": "e", "date": "2026-10-08", "buckets": [{"id": "b", "yes": True, "fee_rate": .05}]}}
        single = flow.follow(events, {"b": tape}, {"e": when + 3600}, ratings, 8, 10, 60, 1800, 50)
        self.assertEqual(single["sharp"]["entries"], 1)
        report = flow.follow({f"e{i}": {"slug": f"e{i}", "date": f"2026-10-{8 + i % 20:02d}",
                                        "buckets": [{"id": f"b{i}", "yes": True, "fee_rate": .05}]} for i in range(40)},
                             {f"b{i}": tape for i in range(40)}, {f"e{i}": when + 3600 for i in range(40)},
                             ratings, 8, 10, 60, 1800, 50)
        entry = report["sharp"]
        self.assertEqual(entry["entries"], 40)
        self.assertAlmostEqual(entry["mean_pnl_per_share"], 1 - .45 - .05 * .45 * .55)
        self.assertAlmostEqual(entry["same_price_upper_bound"], 1 - .40 - .05 * .40 * .60)
        self.assertAlmostEqual(entry["mean_price_slippage"], .05)
        self.assertEqual(entry["before_the_day"]["entries"], 40)

    def test_dead_bucket_needs_an_observation_above_its_range_and_counts_wrong_calls(self):
        settle = self.settle
        local = ["2026-10-07T00:00", "2026-10-08T00:00"]
        stations = {"AAAA": {"local_time": local, "epoch": [T0, T0 + 86400]}}
        event = {"slug": "e", "date": "2026-10-07", "station": "AAAA", "unit": "C", "buckets": [
            {"id": "low", "low": 19.5, "high": 20.5, "yes": False, "fee_rate": .05},
            {"id": "mid", "low": 20.5, "high": 21.5, "yes": False, "fee_rate": .05},
            {"id": "top", "low": 21.5, "high": 22.5, "yes": True, "fee_rate": .05}]}
        observations = {"AAAA": [[T0 - 3600, 30.0], [T0 + 3600, 19.0], [T0 + 7200, 22.0], [T0 + 90000, 35.0]]}
        trades = {"low": [[T0 + 7000, 1, 1, .90, 10.0], [T0 + 8000, 1, 1, .90, 10.0], [T0 + 8050, 0, 1, .10, 5.0]],
                  "mid": [[T0 + 8000, 1, 1, .50, 10.0]], "top": [[T0 + 8000, 0, 0, .60, 10.0]]}
        result = settle.dead_buckets({"e": event}, stations, observations, trades, 600, .02, 1)
        self.assertEqual((result["buckets_declared_dead"], result["declared_dead_but_won"]), (1, 0))   # only `low`: 22 >= 20.5 + 1
        first = result["by_delay"][0]
        self.assertEqual((first["sell_trades"], first["buy_trades_into_dead_bucket"]), (0, 0))        # 200 s after known
        second = result["by_delay"][1]
        self.assertEqual((second["sell_trades"], second["sell_shares"]), (1, 10.0))
        self.assertAlmostEqual(second["taker_gain_usd"], 10 * (.10 - .05 * .90 * .10))
        self.assertEqual(second["buy_trades_into_dead_bucket"], 1)
        loose = settle.dead_buckets({"e": event}, stations, observations, trades, 600, .02, 0)
        self.assertEqual(loose["buckets_declared_dead"], 2)                                           # `mid` too: 22 >= 21.5
        self.assertEqual(settle.reported(22.4, "C"), 22)
        self.assertEqual(settle.reported(25.0, "F"), 77)


if __name__ == "__main__":
    unittest.main()
