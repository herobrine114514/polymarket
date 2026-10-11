from __future__ import annotations
import importlib.util
import math
import unittest

NUMERIC = importlib.util.find_spec("numpy") is not None
T0 = 1_791_331_200


@unittest.skipUnless(NUMERIC, "isolated numeric environment required")
class MakerReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from experiments import weather_maker_backtest as maker
        cls.maker = maker

    def test_quotes_sit_a_margin_from_fair_value_and_never_through_the_midpoint(self):
        quote = self.maker.quote_prices
        self.assertEqual(quote(.30, .30, .05), (.25, .35))
        self.assertEqual(quote(.50, .30, .05), (.29, .55))          # fair above the market: bid capped a tick under mid
        self.assertEqual(quote(.10, .30, .05), (.05, .31))          # fair below the market: offer floored a tick over mid
        self.assertEqual(quote(.333, .333, .05), (.28, .39))        # rounded away from fair value onto the tick grid
        self.assertEqual(quote(.03, .03, .05), (None, .08))         # no bid below one tick
        self.assertEqual(quote(.97, .97, .05), (.92, None))         # no offer above 0.99

    def test_only_taker_flow_strictly_through_the_quote_counts_as_a_fill(self):
        filled = self.maker.filled
        sell_yes, buy_no, buy_yes, sell_no = (0, 0), (1, 1), (0, 1), (1, 0)
        trades = [[T0 + 100, *sell_yes, .20, 6.0], [T0 + 200, *buy_no, .82, 6.0],      # both hit a Yes bid: at .20 and .18
                  [T0 + 300, *buy_yes, .40, 10.0], [T0 + 400, *sell_no, .58, 10.0]]    # both lift a Yes offer: at .40 and .42
        self.assertEqual(filled(trades, T0, T0 + 3600, .25, .35, 10), (True, True))
        self.assertEqual(filled(trades, T0, T0 + 3600, .20, .40, 10), (False, True))   # equal price is queue position, not a fill
        self.assertEqual(filled(trades, T0, T0 + 3600, .19, .42, 10), (False, False))  # only 6 shares traded under .19
        self.assertEqual(filled(trades, T0, T0 + 3600, .19, .42, 5), (True, False))
        self.assertEqual(filled(trades, T0 + 250, T0 + 3600, .25, .35, 10), (False, True))   # earlier trades are before the quote
        self.assertEqual(filled(trades, T0, T0 + 250, .25, .35, 10), (True, False))          # later trades are after it lapsed
        self.assertEqual(filled(trades, T0, T0 + 3600, None, None, 1), (False, False))

    def test_replay_scores_fills_at_resolution_and_skips_events_without_a_full_tape(self):
        maker = self.maker
        base = {"date": "2026-09-01", "lead": 2, "at": T0, "mid": .30, "market": .30, "sharpened": .30, "model": .30, "stack": .30}
        quotes = []
        for day in range(40):
            date = f"2026-09-{day % 28 + 1:02d}" if day < 28 else f"2026-10-{day - 27:02d}"
            quotes.append({**base, "date": date, "event": f"e{day}", "id": f"w{day}", "y": 1.0})
            quotes.append({**base, "date": date, "event": f"e{day}", "id": f"l{day}", "y": 0.0})
        quotes.append({**base, "event": "untaped", "id": "x1", "y": 1.0})
        quotes.append({**base, "event": "untaped", "id": "x2", "y": 0.0})
        trades = {"x1": []}                                          # x2 has no tape: the whole event is left out
        for day in range(40):
            trades[f"w{day}"] = [[T0 + 600, 0, 0, .20, 20.0]]        # someone sold the winner at .20: the .25 bid fills
            trades[f"l{day}"] = [[T0 + 600, 0, 1, .40, 20.0]]        # someone bought the loser at .40: the .35 offer fills
        report = maker.replay(quotes, trades, [.05], [12], 10, 120, 200)
        self.assertEqual(report["events_with_trades"], 40)
        entry = next(item for item in report["results"] if item["variant"] == "stack")
        self.assertEqual((entry["quotes_posted"], entry["fills"]), (160, 80))
        self.assertAlmostEqual(entry["by_side"]["bid"]["mean_pnl_per_share"], 1 - .25)
        self.assertAlmostEqual(entry["by_side"]["offer"]["mean_pnl_per_share"], .35)
        self.assertAlmostEqual(entry["total_pnl_usd_at_size"], 40 * (.75 + .35) * 10)
        early = maker.replay(quotes, trades, [.05], [12], 10, 900, 200)    # quotes not live until after those trades
        self.assertEqual(next(item for item in early["results"] if item["variant"] == "stack")["fills"], 0)

    def test_tape_benchmark_uses_only_trades_before_the_decision(self):
        maker = self.maker
        quotes, trades = [], {}
        for day in range(50):
            date = f"2026-09-{day % 28 + 1:02d}" if day < 28 else f"2026-10-{day - 27:02d}"
            for name, y in (("w", 1.0), ("l", 0.0)):
                quotes.append({"date": date, "lead": 2, "at": T0, "event": f"e{day}", "id": f"{name}{day}", "y": y,
                               "mid": .5, "market": .5, "sharpened": .5, "model": .5, "stack": .5})
            # Before the decision takers paid .80 for the winner; afterwards the tape shows the opposite.
            trades[f"w{day}"] = [[T0 - 3600, 0, 1, .80, 50.0], [T0 + 60, 0, 0, .05, 500.0]]
            trades[f"l{day}"] = [[T0 - 3600, 1, 1, .80, 50.0], [T0 + 60, 1, 0, .05, 500.0], [T0 - 9 * 3600, 0, 1, .99, 500.0]]
        entry = maker.tape_benchmark(quotes, trades, 6, 200)["lead2"]
        self.assertEqual(entry["events"], 50)
        self.assertAlmostEqual(entry["scores"]["tape_log_loss"], -math.log(.8), places=6)   # .80 Yes vs .20 implied by No at .80
        self.assertAlmostEqual(entry["scores"]["market_log_loss"], math.log(2), places=6)
        self.assertGreater(entry["versus_tape"]["stack"]["log_loss"]["ci95_by_day"][0], 0)
        empty = {key: [] for key in trades}
        self.assertIn("skipped", maker.tape_benchmark(quotes, empty, 6, 200)["lead2"])


if __name__ == "__main__":
    unittest.main()
