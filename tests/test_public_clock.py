from __future__ import annotations
import unittest
from polycast.public_clock import clock_intersection


class PublicClockTests(unittest.TestCase):
    def samples(self):
        return [{"venue": "polymarket", "sent_local_ms": 1000000, "received_local_ms": 1000400,
                 "server_ms": 997000, "precision_ms": 1000, "monotonic_elapsed_ms": 400},
                {"venue": "coinbase", "sent_local_ms": 1000500, "received_local_ms": 1000900,
                 "server_ms": 998000, "precision_ms": 1, "monotonic_elapsed_ms": 400}]

    def test_uncertainty_accounts_for_roundtrip_and_integer_seconds(self):
        result = clock_intersection(self.samples())
        self.assertEqual(result["status"], "venues_agree")
        self.assertAlmostEqual(result["offset_ms"], -2699.5)
        self.assertAlmostEqual(result["uncertainty_ms"], 200.5)

    def test_disagreement_single_venue_or_wall_step_is_unverified(self):
        samples = self.samples()
        with self.assertRaises(ValueError):
            clock_intersection(samples[:1])
        samples[1]["server_ms"] += 10000
        with self.assertRaises(ValueError):
            clock_intersection(samples)
        samples = self.samples()
        samples[0]["monotonic_elapsed_ms"] = 1000
        with self.assertRaises(ValueError):
            clock_intersection(samples)

    def test_slow_roundtrip_and_large_clock_offset_do_not_authorize_freezing(self):
        samples = self.samples()
        samples[0]["received_local_ms"] += 3000
        samples[0]["monotonic_elapsed_ms"] += 3000
        with self.assertRaises(ValueError):
            clock_intersection(samples)
        samples = self.samples()
        for row in samples:
            row["server_ms"] -= 15000
        with self.assertRaises(ValueError):
            clock_intersection(samples)


if __name__ == "__main__":
    unittest.main()
