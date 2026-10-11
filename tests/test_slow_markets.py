from __future__ import annotations
import importlib.util
import math
import unittest

from experiments.slow_market_backfill import market_row, parse, stamp
from experiments.weather_edge import parse_bucket, shift_date, weather_events

NUMERIC = all(importlib.util.find_spec(name) for name in ("numpy", "scipy"))
DAY = 86400
T0 = 1_791_331_200  # 2026-10-07T00:00:00Z


class SlowMarketParsingTests(unittest.TestCase):
    def test_gamma_timestamps_are_read_as_utc_regardless_of_local_zone(self):
        self.assertEqual(parse("2026-10-07T00:00:00Z"), T0)
        self.assertEqual(parse("2026-10-07 00:00:00+00"), T0)
        self.assertEqual(parse("2026-10-06T17:52:16.148591Z"), T0 - 6 * 3600 - 7 * 60 - 44)
        self.assertEqual(stamp(T0), "2026-10-07T00:00:00Z")
        self.assertIsNone(parse(None))
        self.assertIsNone(parse("not a time"))

    def test_market_row_keeps_binary_markets_with_fee_and_event(self):
        market = {"id": 9, "question": "Q", "slug": "q", "conditionId": "0x1", "clobTokenIds": '["1", "2"]',
                  "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]', "volumeNum": 12000,
                  "startDate": "2026-10-05T00:00:00Z", "endDate": "2026-10-07T00:00:00Z",
                  "closedTime": "2026-10-07 03:00:00+00", "feeType": "weather_fees",
                  "feeSchedule": {"rate": .05, "exponent": 1}, "events": [{"id": 77, "slug": "e", "seriesSlug": "s"}]}
        row = market_row(market)
        self.assertEqual((row["id"], row["end"], row["closed_at"], row["fee_rate"], row["event_id"], row["outcome_prices"]),
                         ("9", T0, T0 + 3 * 3600, .05, "77", ["1", "0"]))
        self.assertIsNone(market_row({**market, "clobTokenIds": '["1", "2", "3"]'}))
        self.assertIsNone(market_row({"id": 1}))

    def test_temperature_buckets_and_dates(self):
        self.assertEqual(parse_bucket("27°C"), (26.5, 27.5, "C"))
        self.assertEqual(parse_bucket("76-77°F"), (75.5, 77.5, "F"))
        self.assertEqual(parse_bucket("30°C or higher"), (29.5, math.inf, "C"))
        self.assertEqual(parse_bucket("-2°C or below"), (-math.inf, -1.5, "C"))
        self.assertIsNone(parse_bucket("Japan"))
        self.assertEqual(shift_date("2026-10-01", -1), "2026-09-30")
        self.assertEqual(shift_date("2026-03-08", 1), "2026-03-09")

    def test_weather_events_group_buckets_by_station_and_day(self):
        base = {"event_slug": "highest-temperature-in-paris-on-august-27-2026", "outcomes": ["Yes", "No"],
                "resolution_source": "https://www.weather.gov/wrh/timeseries?site=lfpb", "fee_rate": .05, "volume": 1}
        markets = [{**base, "id": "a", "group_item": "27°C", "outcome_prices": ["1", "0"]},
                   {**base, "id": "b", "group_item": "28°C", "outcome_prices": ["0", "1"]},
                   {**base, "id": "c", "group_item": "Japan", "outcome_prices": ["0", "1"]},
                   {**base, "id": "d", "group_item": "26°C", "outcome_prices": ["0.5", "0.5"]},
                   {**base, "id": "e", "group_item": "25°C", "outcome_prices": ["0", "1"],
                    "resolution_source": "https://www.weather.gov.hk/en/cis/climat.htm"}]
        events = weather_events(markets)
        self.assertEqual(list(events), [base["event_slug"]])
        event = events[base["event_slug"]]
        self.assertEqual((event["station"], event["date"], event["unit"]), ("LFPB", "2026-08-27", "C"))
        self.assertEqual([(bucket["id"], bucket["yes"]) for bucket in event["buckets"]], [("a", True), ("b", False)])


@unittest.skipUnless(NUMERIC, "isolated numeric environment required")
class SlowMarketModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        from experiments import slow_market_calibration as calibration
        from experiments import weather_edge_model as weather
        cls.np, cls.calibration, cls.weather = np, calibration, weather

    def test_price_is_sampled_only_while_the_market_is_open_and_never_from_the_future(self):
        market = {"id": "m", "event_id": "e", "outcome_prices": ["1", "0"], "outcomes": ["Yes", "No"],
                  "fee_type": "politics_fees", "fee_rate": .04, "slug": "x", "start": T0 - 2 * DAY,
                  "end": T0, "closed_at": T0 - 2 * 3600, "game_start": None, "volume": 1}
        history = [[T0 - DAY - 600, .30], [T0 - DAY + 600, .90], [T0 - 6 * 3600 - 60, .40], [T0 - 3600 - 60, .99]]
        rows = self.calibration.sample_rows([market], {"m": history}, max_gap=3 * 3600)
        sampled = {row["horizon"]: row["price"] for row in rows}
        # 7d/3d: before the market opened; 1h: already closed; 1d: the later 0.90 print must not leak in.
        self.assertEqual(sampled, {"1d": .30, "6h": .40})
        self.assertTrue(all(row["category"] == "politics" and row["y"] == 1.0 for row in rows))
        self.assertEqual(self.calibration.sample_rows([market], {"m": history}, max_gap=30), [])
        sport = {**market, "fee_type": "sports_fees_v3", "sports_type": "moneyline", "game_start": T0, "closed_at": T0 + 9000}
        self.assertEqual({row["horizon"] for row in self.calibration.sample_rows([sport], {"m": history}, max_gap=3 * 3600)},
                         {"24h", "6h", "1h", "10m"})          # still open before the game: all four horizons
        self.assertEqual(self.calibration.category({"fee_type": "crypto_fees_v2", "slug": "btc-updown-5m-1"}), "crypto:updown")
        self.assertEqual(self.calibration.category({"fee_type": "crypto_fees_v2", "slug": "bitcoin-above-80k"}), "crypto:other")

    def test_calibration_fit_recovers_an_under_confident_market(self):
        np = self.np
        rng = np.random.default_rng(3)
        price = rng.uniform(.05, .95, 40000)
        truth = 1 / (1 + np.exp(-(.2 + 1.5 * np.log(price / (1 - price)))))
        intercept, slope = self.calibration.fit_calibration(price, (rng.uniform(size=len(price)) < truth).astype(float))
        self.assertAlmostEqual(intercept, .2, delta=.06)
        self.assertAlmostEqual(slope, 1.5, delta=.06)

    def test_forecast_maximum_uses_only_hours_up_to_the_decision_hour(self):
        weather = self.weather
        local = [f"2026-08-{day:02d}T{hour:02d}:00" for day in (25, 26, 27) for hour in range(24)]
        station = {"local_time": local, "epoch": [T0 + 3600 * index for index in range(len(local))], "forecast": {}}
        for lead in (1, 2):
            for model in weather.MODELS[:3]:
                station["forecast"][f"temperature_2m_previous_day{lead}_{model}"] = [
                    40.0 if stamp[11:13] == "21" else 20.0 + int(stamp[11:13]) / 10 + lead for stamp in local]
        day = weather.daily_forecasts(station, 18)["2026-08-27"]
        self.assertEqual([value for value in day["lead1"] if not math.isnan(value)], [22.8] * 3)   # 21:00 spike excluded
        self.assertEqual(sum(math.isnan(value) for value in day["lead1"]), len(weather.MODELS) - 3)
        self.assertEqual(day["lead1_decision"], station["epoch"][local.index("2026-08-26T18:00")])
        self.assertEqual(day["lead2_decision"], station["epoch"][local.index("2026-08-25T18:00")])
        self.assertNotIn("lead2", weather.daily_forecasts(station, 18)["2026-08-26"])            # no decision time on record

    def test_walk_forward_mean_ignores_observations_not_complete_at_the_decision(self):
        weather, np = self.weather, self.np
        rng = np.random.default_rng(7)
        dates = [shift_date("2026-07-01", offset) for offset in range(40)]
        width = len(weather.MODELS)
        targets = {date: list(25 + rng.normal(0, 2, width)) for date in dates}
        observed = {date: float(np.mean(targets[date]) + 1.0 + rng.normal(0, .5)) for date in dates}
        target = dates[-1]
        base = weather.walk_forward_means(targets, observed, 1, 10)[target]
        self.assertAlmostEqual(base["baseline"] - float(np.mean(targets[target])), 1.0, delta=.4)   # learned the +1 bias
        late = dict(observed)
        late[target] += 50
        late[shift_date(target, -1)] += 50                 # yesterday is still in progress at the decision time
        self.assertEqual(weather.walk_forward_means(targets, late, 1, 10)[target], base)
        early = dict(observed)
        early[shift_date(target, -2)] += 50
        self.assertNotEqual(weather.walk_forward_means(targets, early, 1, 10)[target], base)
        self.assertNotIn(dates[5], weather.walk_forward_means(targets, observed, 1, 10))            # too little history
        # The error-so-far term reads only the decision day's partial observation.
        config = {"window": 90, "half_life": 10, "power": 2.0, "ar": .4}
        yesterday = shift_date(target, -1)
        plain = weather.walk_forward_means(targets, observed, 1, 10, {**config, "ar": 0.0})[target]["weighted"]
        warm = weather.walk_forward_means(targets, observed, 1, 10, config, {yesterday: 99.0, target: -99.0})[target]["weighted"]
        cool = weather.walk_forward_means(targets, observed, 1, 10, config, {yesterday: 99.0, target: 55.0})[target]["weighted"]
        self.assertGreater(warm, plain + 10)
        self.assertEqual(warm, cool)
        self.assertEqual(weather.walk_forward_means(targets, observed, 1, 10, config, {target: 99.0})[target]["weighted"], plain)

    def test_error_scale_reads_only_residuals_before_the_cutoff(self):
        weather = self.weather
        items = [(shift_date("2026-07-01", index // 10), f"S{index % 10}", .5, 1.0) for index in range(400)]
        items += [(shift_date("2026-09-01", 0), "S0", .5, 30.0)] * 50
        residuals = weather.Residuals(items)
        self.assertIsNone(residuals.scale("S0", "2026-07-10", 45, 10, None))                 # under 300 past errors
        self.assertAlmostEqual(residuals.scale("S0", "2026-09-01", 45, 10, .5), 1.0, places=9)
        self.assertGreater(residuals.scale("S0", "2026-09-02", 45, 10, None), 5)
        self.assertAlmostEqual(weather.bucket_probability("baseline", -math.inf, math.inf, 20, 1), .999)
        self.assertLess(weather.bucket_probability("baseline", 22.5, 23.5, 20, 1),
                        weather.bucket_probability("weighted", 22.5, 23.5, 20, 1))              # heavier tails

    def test_stack_is_chosen_from_earlier_days_only_and_needs_every_bucket(self):
        weather, np = self.weather, self.np
        rng = np.random.default_rng(1)
        part = []
        for day in range(40):
            date = shift_date("2026-08-01", day)
            for city in range(8):
                winner = int(rng.integers(0, 5))
                for bucket in range(5):
                    # The model knows the winner; the market is flat. Any sensible stack must lean on the model.
                    part.append({"event": f"{date}-{city}", "date": date, "lead": 1, "low": float(bucket), "buckets": 5,
                                 "price": .2, "weighted": .6 if bucket == winner else .1, "y": float(bucket == winner),
                                 "fee_rate": .05, "id": f"{date}-{city}-{bucket}", "at": 0})
        result = weather.event_level(part, np.random.default_rng(2), 200, .01)
        self.assertEqual(result["events"], 320)
        self.assertEqual(result["events_scored_after_warmup"], 320 - 8 * 20)        # 150 known events need 19 full earlier days
        self.assertLess(result["final_stack_setting"]["market_share"], .5)
        self.assertLess(result["versus_normalized_market"]["stack"]["log_loss"]["ci95_by_day"][1], 0)
        self.assertAlmostEqual(result["scores"]["market"]["log_loss"], math.log(5), places=6)
        incomplete = [row for row in part if not row["id"].endswith("-4")]           # one bucket of every event unpriced
        self.assertIn("skipped", weather.event_level(incomplete, np.random.default_rng(2), 200, .01))
        self.assertIn("skipped", weather.event_level(part[:5 * 8 * 12], np.random.default_rng(2), 200, .01))


if __name__ == "__main__":
    unittest.main()
