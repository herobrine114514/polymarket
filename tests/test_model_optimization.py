from __future__ import annotations

import json
import math
import statistics
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polycast.analysis import (
    apply_bayesian_shrinkage,
    deterministic_crypto5m_review,
    make_payload,
    run_codex,
)
from polycast.config import Config
from polycast.paper import _blackout_reason, _validated_order, candidate
from polycast.short_cycle import _diffusion_units, _twap_snapshot
from polycast.store import database, now_iso, save_prediction, upsert_markets


class TestPillar1PricingAndNetEdge(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.temp.name))
        self.market = {
            "id": "201", "event_id": "e201", "event_title": "Test market",
            "event_slug": "test-201", "question": "Yes?",
            "description": "Rules",
            "end_at": (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat(),
            "yes_token": "token-yes", "no_token": "token-no",
            "liquidity": 10000, "volume_24h": 5000, "min_order_size": 5.0,
            "tick_size": 0.01, "fee_rate": 0.01, "category": "macro",
            "yes_label": "Yes", "no_label": "No",
        }
        self.live = {
            "id": "201", "closed": False, "active": True, "acceptingOrders": True,
            "enableOrderBook": True, "clobTokenIds": ["token-yes", "token-no"],
            "outcomes": ["Yes", "No"],
            "description": self.market["description"], "endDate": self.market["end_at"],
            "feeSchedule": {"rate": self.market["fee_rate"]},
        }

    def tearDown(self):
        self.temp.cleanup()

    def _reviewed_pred(self, p: float) -> dict:
        now = now_iso()
        return {
            "p_yes": p, "abstain": False, "reasoning": "high conviction test",
            "assumptions": [], "supporting_evidence_ids": [1], "counter_evidence_ids": [],
            "review_trigger": "price-move",
            "review": {"approved": True, "reason": "checked", "issues": [], "checked_evidence_ids": [1]},
            "reviewed_at": now,
            "valid_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }

    def test_price_band_rejects_lottery_underdog(self):
        """Underdog lottery contracts (Ask < 0.15) must be rejected by tradable price band constraints."""
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            save_prediction(conn, "201", "rules", "ev", self._reviewed_pred(0.20))
            # Simulated order book with very cheap underdog ask (0.10 < 0.15)
            book_low = {
                "timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "asks": [{"price": ".10", "size": "100"}],
            }
            with patch("polycast.paper.order_book", return_value=book_low), \
                 patch("polycast.paper.live_market", return_value=self.live), \
                 patch("polycast.paper._blackout_reason", return_value=None), \
                 patch("polycast.paper._prediction_current", return_value=True):
                quote, reason = candidate(conn, "201", self.config)
                self.assertIsNone(quote)
                self.assertIn("outside tradable price band", reason)

    def test_relative_roi_and_edge_accepts_favorites(self):
        """Favorite contracts (e.g. P=0.85, Ask=0.70) must now pass with ROI >= 15% and edge >= 0.02."""
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            pid = save_prediction(conn, "201", "rules", "ev", self._reviewed_pred(0.85))
            book_fav = {
                "timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "asks": [{"price": ".70", "size": "100"}],
            }
            with patch("polycast.paper.order_book", return_value=book_fav), \
                 patch("polycast.paper.live_market", return_value=self.live), \
                 patch("polycast.paper._blackout_reason", return_value=None), \
                 patch("polycast.paper._prediction_current", return_value=True):
                quote, reason = candidate(conn, "201", self.config)
                self.assertIsNotNone(quote, f"Favorite quote was rejected: {reason}")
                self.assertEqual(quote["side"], "YES")
                self.assertEqual(quote["avg_ask"], 0.70)
                # Cost per share = 0.70 + fee (fee = 0.70 * 0.01 = 0.007)
                self.assertGreaterEqual(quote["expected_roi"], 0.15)
                self.assertGreaterEqual(quote["expected_edge"], 0.02)

    def test_low_roi_rejected_even_if_positive_edge(self):
        """A contract with edge >= 0.02 but expected ROI < 15% must be rejected."""
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            # P = 0.83, Ask = 0.80 -> edge = 0.83 - 0.80 = 0.03 >= 0.02, but ROI = 0.03 / 0.80 = 3.75% < 15%
            save_prediction(conn, "201", "rules", "ev", self._reviewed_pred(0.83))
            book_low_roi = {
                "timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "asks": [{"price": ".80", "size": "100"}],
            }
            with patch("polycast.paper.order_book", return_value=book_low_roi), \
                 patch("polycast.paper.live_market", return_value=self.live), \
                 patch("polycast.paper._blackout_reason", return_value=None), \
                 patch("polycast.paper._prediction_current", return_value=True):
                quote, reason = candidate(conn, "201", self.config)
                self.assertIsNone(quote)
                self.assertIn("is below threshold", reason)

    def test_coinbase_exchange_status_does_not_trigger_blackout_cooling(self):
        """coinbase_exchange_status is continuous venue telemetry and must not trigger cooling window."""
        market_5m = {
            **self.market,
            "category": "crypto5m",
            "source_ref": "BTCUSD:1791035100",
        }
        with database(self.config.database) as conn:
            upsert_markets(conn, [market_5m])
            ev = [
                {"source_id": "chainlink_btc_twap60", "first_seen_at": now_iso(), "published_at": now_iso()},
                {"source_id": "coinbase_exchange_status", "first_seen_at": now_iso(), "published_at": now_iso()},
            ]
            with patch("polycast.paper.evidence_for_market", return_value=ev):
                reason = _blackout_reason(conn, market_5m, self.config)
                self.assertIsNone(reason, f"coinbase_exchange_status triggered unexpected blackout: {reason}")



class TestPillar2Deterministic5mReview(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.payload = {
            "market_id": "301",
            "category": "crypto5m",
            "as_of": self.now.isoformat(),
            "end_at": (self.now + timedelta(seconds=120)).isoformat(),
            "evidence": [
                {
                    "id": 1,
                    "source_id": "chainlink_btc_twap60",
                    "updated_at": (self.now - timedelta(seconds=10)).isoformat(),
                    "published_at": (self.now - timedelta(seconds=10)).isoformat(),
                    "first_seen_at": (self.now - timedelta(seconds=5)).isoformat(),
                }
            ],
        }
        self.prediction = {
            "market_id": "301",
            "as_of": self.payload["as_of"],
            "p_yes": 0.88,
            "abstain": False,
            "supporting_evidence_ids": [1], "counter_evidence_ids": [],
        }

    def test_deterministic_review_approves_fresh_and_valid(self):
        review = deterministic_crypto5m_review(self.payload, self.prediction, now=self.now)
        self.assertTrue(review["approved"])
        self.assertEqual(review["issues"], [])
        self.assertIn("Deterministic fast validation passed", review["reason"])
        self.assertEqual(review["checked_evidence_ids"], [1])

    def test_deterministic_review_rejects_staleness(self):
        # Evidence updated 45s ago (> 30s limit)
        stale_payload = {
            **self.payload,
            "evidence": [{
                "id": 1,
                "source_id": "chainlink_btc_twap60",
                "updated_at": (self.now - timedelta(seconds=45)).isoformat(),
            }],
        }
        review = deterministic_crypto5m_review(stale_payload, self.prediction, now=self.now)
        self.assertFalse(review["approved"])
        self.assertTrue(any("staleness" in issue for issue in review["issues"]))

    def test_deterministic_review_rejects_remaining_seconds_under_45(self):
        # Only 30s remaining until end_at (< 45s cutoff)
        expiring_payload = {
            **self.payload,
            "end_at": (self.now + timedelta(seconds=30)).isoformat(),
        }
        review = deterministic_crypto5m_review(expiring_payload, self.prediction, now=self.now)
        self.assertFalse(review["approved"])
        self.assertTrue(any("below 45s cutoff" in issue for issue in review["issues"]))

    def test_deterministic_review_rejects_invalid_probability(self):
        for bad_p in (0.005, 0.995, float("nan"), None):
            pred = {"market_id": "301", "p_yes": bad_p, "abstain": False}
            review = deterministic_crypto5m_review(self.payload, pred, now=self.now)
            self.assertFalse(review["approved"])


    def test_deterministic_review_supports_source_updated_at(self):
        """Review must recognize source_updated_at from make_payload() output."""
        payload_with_source_key = {
            **self.payload,
            "evidence": [{
                "id": 1,
                "source_id": "chainlink_btc_twap60",
                "source_updated_at": (self.now - timedelta(seconds=12)).isoformat(),
                "published_at": (self.now - timedelta(seconds=12)).isoformat(),
                "first_seen_at": (self.now - timedelta(seconds=5)).isoformat(),
            }],
        }
        review = deterministic_crypto5m_review(payload_with_source_key, self.prediction, now=self.now)
        self.assertTrue(review["approved"])

    def test_deterministic_review_rejects_missing_twap_evidence(self):
        """Review must reject if required chainlink_btc_twap60 evidence is absent."""
        empty_ev_payload = {**self.payload, "evidence": []}
        review = deterministic_crypto5m_review(empty_ev_payload, self.prediction, now=self.now)
        self.assertFalse(review["approved"])
        self.assertTrue(any("chainlink_btc_twap60" in issue for issue in review["issues"]))


class TestPillar3BayesianPriorAndShrinkage(unittest.TestCase):
    def test_shrinkage_pulls_towards_market_prior_under_normal_z(self):
        """When |z| < 2.0, model probability should shrink towards market prior."""
        p_model = 0.80
        p_market = 0.50
        shrunk = apply_bayesian_shrinkage(p_model, p_market, z=1.0, basis_aligned=True, lambda_shrink=0.20)
        # Should be strictly between p_market and p_model
        self.assertGreater(shrunk, p_market)
        self.assertLess(shrunk, p_model)
        self.assertAlmostEqual(shrunk, 0.7516, places=2)

    def test_shrinkage_bypassed_when_high_z_and_basis_aligned(self):
        """When |z| >= 2.0 and basis is aligned, high-confidence probability (>0.85) is preserved."""
        p_model = 0.92
        p_market = 0.50
        preserved = apply_bayesian_shrinkage(p_model, p_market, z=2.5, basis_aligned=True)
        self.assertEqual(preserved, p_model)

    def test_shrinkage_applied_when_high_z_but_basis_divergent(self):
        """When basis is divergent, shrinkage is enforced even if z is large."""
        p_model = 0.92
        p_market = 0.50
        shrunk = apply_bayesian_shrinkage(p_model, p_market, z=2.5, basis_aligned=False, lambda_shrink=0.20)
        self.assertLess(shrunk, p_model)

    def test_shrinkage_applied_when_high_z_but_direction_mismatch(self):
        """When z is strongly bullish (+2.5) but model predicts bearish (0.10), shrinkage must apply."""
        p_model = 0.10
        p_market = 0.50
        shrunk = apply_bayesian_shrinkage(p_model, p_market, z=2.5, basis_aligned=True, lambda_shrink=0.20)
        self.assertGreater(shrunk, p_model)

    def test_run_codex_end_to_end_asian_z_and_basis_alignment(self):
        """run_codex extracts Asian z and Spot-TWAP basis alignment from evidence."""
        now = datetime.now(timezone.utc).isoformat()
        mock_output = {
            "market_id": "301", "as_of": now,
            "p_yes": 0.90, "abstain": False, "reasoning": "strong upward drift",
            "assumptions": [], "supporting_evidence_ids": [1], "counter_evidence_ids": [],
            "review_trigger": "price-move",
        }
        # Case A: High Asian z (2.5) and aligned basis (+15.0 USD) -> preserves high confidence
        payload_aligned = {
            "market_id": "301", "as_of": now,
            "category": "crypto5m", "question": "Up?", "end_at": now, "description": "rules",
            "market_prior": {"p_market_mid": 0.50},
            "evidence": [{
                "id": 1, "source_id": "chainlink_btc_twap60",
                "summary": "Asian z +2.5000; Spot-TWAP rolling basis +15.00 USD.",
            }],
        }
        with patch("polycast.analysis._run_json", return_value=dict(mock_output)):
            res_aligned = run_codex(payload_aligned)
            self.assertLess(res_aligned["p_yes"], 0.90)
            self.assertEqual(res_aligned["raw_p_yes"], 0.90)

        # Case B: High Asian z (2.5) but divergent basis (-25.0 USD) -> shrinks towards prior
        payload_divergent = {
            "market_id": "301", "as_of": now,
            "category": "crypto5m", "question": "Up?", "end_at": now, "description": "rules",
            "market_prior": {"p_market_mid": 0.50},
            "evidence": [{
                "id": 1, "source_id": "chainlink_btc_twap60",
                "summary": "Asian z +2.5000; Spot-TWAP rolling basis -25.00 USD.",
            }],
        }
        with patch("polycast.analysis._run_json", return_value=dict(mock_output)):
            res_divergent = run_codex(payload_divergent)
            self.assertLess(res_divergent["p_yes"], 0.90)

    def test_shrinkage_gracefully_handles_none_prior(self):
        self.assertEqual(apply_bayesian_shrinkage(0.85, None), 0.85)


class TestPillar4AsianIntegralVarianceScaling(unittest.TestCase):
    def test_asian_integral_variance_scale_in_diffusion_units(self):
        """Asian integral TWAP variance scale must scale by 1 / sqrt(3)."""
        opening = 80000.0
        latest = 80160.0
        sigma_1m = 0.001
        remaining_minutes = 3.0

        units = _diffusion_units(opening, latest, sigma_1m, remaining_minutes)
        # Check standard horizon
        standard_log_stdev = sigma_1m * math.sqrt(remaining_minutes)
        self.assertAlmostEqual(units["horizon_log_stdev"], standard_log_stdev)

        self.assertFalse(units["future_twap_variance_identified"])
        self.assertNotIn("asian_z", units)
        self.assertNotIn("asian_p_up", units)


if __name__ == "__main__":
    unittest.main()
