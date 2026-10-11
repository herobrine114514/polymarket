"""Regression cases for bugs found in the Antigravity handover audit."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from polycast.analysis import apply_bayesian_shrinkage, deterministic_crypto5m_review
from polycast.config import Config
from polycast.execution import FEE_METHOD, fill_totals
from polycast.paper import _validated_order
from polycast.store import database, now_iso, save_prediction, upsert_markets


class CausalEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 3, 16, tzinfo=timezone.utc)
        as_of = (self.now - timedelta(seconds=5)).isoformat()
        observed = (self.now - timedelta(seconds=10)).isoformat()
        self.payload = {"market_id": "1", "as_of": as_of,
                        "end_at": (self.now + timedelta(seconds=90)).isoformat(),
                        "evidence": [{"id": 1, "source_id": "chainlink_btc_twap60",
                                      "published_at": observed, "source_updated_at": observed,
                                      "first_seen_at": as_of}]}
        self.prediction = {"market_id": "1", "as_of": as_of, "p_yes": .7, "abstain": False,
                           "supporting_evidence_ids": [1], "counter_evidence_ids": []}

    def review(self, payload=None, prediction=None):
        return deterministic_crypto5m_review(self.payload if payload is None else payload,
                                            self.prediction if prediction is None else prediction,
                                            now=self.now)

    def test_valid_envelope_does_not_claim_reasoning_validation(self):
        value = self.review()
        self.assertTrue(value["approved"])
        self.assertFalse(value["reasoning_independently_verified"])

    def test_wrong_market_or_forecast_time_rejected(self):
        for field, value in (("market_id", "other"), ("as_of", self.now.isoformat())):
            with self.subTest(field=field):
                self.assertFalse(self.review(prediction={**self.prediction, field: value})["approved"])

    def test_future_forecast_has_no_five_second_grace(self):
        future = (self.now + timedelta(seconds=1)).isoformat()
        self.assertFalse(self.review({**self.payload, "as_of": future},
                                     {**self.prediction, "as_of": future})["approved"])

    def test_evidence_unavailable_at_forecast_even_when_fresh_now(self):
        for field in ("first_seen_at", "published_at", "source_updated_at"):
            payload = deepcopy(self.payload)
            payload["evidence"][0][field] = (self.now - timedelta(seconds=1)).isoformat()
            with self.subTest(field=field):
                self.assertFalse(self.review(payload)["approved"])

    def test_missing_availability_timestamps_rejected(self):
        for field in ("first_seen_at", "published_at", "source_updated_at"):
            payload = deepcopy(self.payload)
            del payload["evidence"][0][field]
            with self.subTest(field=field):
                self.assertFalse(self.review(payload)["approved"])

    def test_duplicate_boolean_or_unknown_evidence_ids_rejected(self):
        duplicate = deepcopy(self.payload)
        duplicate["evidence"].append(deepcopy(duplicate["evidence"][0]))
        self.assertFalse(self.review(duplicate)["approved"])
        boolean = deepcopy(self.payload)
        boolean["evidence"][0]["id"] = True
        self.assertFalse(self.review(boolean)["approved"])
        for values in ([True], [999], "1", None):
            with self.subTest(values=values):
                self.assertFalse(self.review(prediction={**self.prediction,
                                            "supporting_evidence_ids": values})["approved"])

    def test_malformed_envelopes_reject_without_crashing(self):
        for payload in (None, {}, {**self.payload, "as_of": 123},
                        {**self.payload, "end_at": []}, {**self.payload, "evidence": [None]},
                        {**self.payload, "evidence": "bad"}):
            with self.subTest(payload=payload):
                value = deterministic_crypto5m_review(payload, self.prediction, now=self.now)
                self.assertFalse(value["approved"])

    def test_unverified_high_z_cannot_implicitly_bypass_shrinkage(self):
        self.assertLess(apply_bayesian_shrinkage(.92, .5, z=9), .92)
        for value in (float("nan"), float("inf"), True, -.1, 1.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                apply_bayesian_shrinkage(.8, .5, lambda_shrink=value)


class QuoteGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(data_dir=Path(self.temp.name))
        self.market = {"id": "1", "event_id": "event", "event_title": "Test",
                       "event_slug": "test", "question": "Up?", "description": "Rules",
                       "end_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                       "yes_token": "up", "no_token": "down", "liquidity": 10000,
                       "volume_24h": 1000, "min_order_size": 5., "tick_size": .01,
                       "fee_rate": .01, "category": "crypto5m"}
        for name, value in (("_blackout_reason", None), ("_prediction_current", True),
                            ("_joint_event_reason", None), ("assert_entry_allowed", None)):
            mock = patch("polycast.paper." + name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def quote(self, conn, p=.63):
        upsert_markets(conn, [self.market])
        identifier = save_prediction(conn, "1", "rules", "evidence",
                       {"as_of": now_iso(), "p_yes": p, "abstain": False,
                        "review": {"approved": True}, "reviewed_at": now_iso(),
                        "valid_until": self.market["end_at"]})
        fills = [{"price": .6, "shares": 5.}]
        gross, price, fee = fill_totals(fills, 5, .01)
        return {"market_id": "1", "event_id": "event", "risk_group": "btc-5m",
                "prediction_id": identifier, "side": "YES", "captured_at": now_iso(),
                "book_timestamp": now_iso(), "shares": 5., "avg_ask": price, "worst_ask": price,
                "fee_per_share": fee / 5, "fee_method": FEE_METHOD, "fills": fills,
                "total_cost": gross + fee, "expected_edge": p - price - fee / 5}

    def test_missing_roi_does_not_bypass_minimum(self):
        with database(self.config.database) as conn:
            with self.assertRaisesRegex(ValueError, "ROI is below"):
                _validated_order(conn, self.quote(conn), self.config)

    def test_forged_or_nan_roi_does_not_bypass_minimum(self):
        for roi in (.99, float("nan"), float("inf")):
            with self.subTest(roi=roi), database(self.config.database) as conn:
                quote = self.quote(conn)
                quote["expected_roi"] = roi
                with self.assertRaisesRegex(ValueError, "ROI mismatch"):
                    _validated_order(conn, quote, self.config)

    def test_legacy_raw_edge_cannot_bypass_current_haircut(self):
        with database(self.config.database) as conn:
            quote = self.quote(conn, p=.70)
            with self.assertRaisesRegex(ValueError, "edge is below"):
                _validated_order(conn, quote, replace(self.config, probability_haircut=.1))

    def test_nonfinite_financial_configuration_rejected(self):
        for field in ("initial_cash", "min_roi", "min_liquidity", "min_edge"):
            for value in (float("nan"), float("inf"), True):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(self.config, **{field: value}).validate()


if __name__ == "__main__":
    unittest.main()
