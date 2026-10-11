from __future__ import annotations

import math
import unittest

from polycast.analysis import make_payload
from polycast.short_cycle import _diffusion_units


class CryptoUnitTests(unittest.TestCase):
    def test_one_minute_sigma_scales_in_minutes_and_dollars(self):
        # 1% per minute at $10,000 means $100 over one minute and
        # $200 over four minutes. Treating minutes as hours fails these checks.
        one = _diffusion_units(10000, 10000, .01, 1)
        four = _diffusion_units(10000, 10000, .01, 4)
        self.assertAlmostEqual(one["horizon_usd_stdev"], 100)
        self.assertAlmostEqual(four["horizon_usd_stdev"], 200)
        self.assertAlmostEqual(four["horizon_log_stdev"], .02)
        self.assertEqual(four["p_up"], .5)
        positive = _diffusion_units(10000, 10000 * math.exp(.02), .01, 4)
        self.assertAlmostEqual(positive["z"], 1)
        self.assertAlmostEqual(positive["p_up"], .841344746, places=8)
        for invalid in (0, -1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _diffusion_units(10000, 10000, .01, invalid)

    def test_explicit_utc_window_does_not_reinterpret_et_title(self):
        market = {"id": "1", "event_title": "Bitcoin - 1:25AM-1:30AM ET",
                  "question": "Up?", "description": "TWAP rules", "category": "crypto5m",
                  "source_ref": "BTCUSD:1790745900", "end_at": "2026-09-30T05:30:00Z"}
        payload = make_payload(market, [], "2026-09-30T13:26:00+08:00", "deepseek")
        timing = payload["window_timing_utc"]
        self.assertEqual(timing["start_at"], "2026-09-30T05:25:00+00:00")
        self.assertEqual(timing["end_at"], "2026-09-30T05:30:00+00:00")
        self.assertEqual(timing["forecast_at"], "2026-09-30T05:26:00+00:00")
        self.assertEqual(timing["remaining_seconds"], 240)
