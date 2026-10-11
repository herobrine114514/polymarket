from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from experiments.btc5m_model_diagnostics import build_report
from polycast.store import (database, save_analysis_draft, save_analysis_market_baseline,
                            save_market_baseline, save_prediction, upsert_markets)


class Btc5mDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.market = {"id": "1", "event_id": "event-1", "event_title": "BTC 5m",
                       "event_slug": "btc-test", "question": "BTC up?", "description": "TWAP",
                       "end_at": "2026-09-01T12:05:00Z", "yes_token": "up", "no_token": "down",
                       "liquidity": 10000, "volume_24h": 1000, "min_order_size": 5,
                       "tick_size": .01, "fee_rate": 0, "category": "crypto5m"}
        self.cutoff = datetime(2026, 9, 1, 13, tzinfo=timezone.utc)
        with database(self.path) as conn:
            upsert_markets(conn, [self.market])
            conn.execute("INSERT INTO resolutions VALUES(?,?,?,?,?)",
                         ("1", "YES", "https://gamma-api.polymarket.com/test",
                          "2026-09-01T12:06:00Z", "2026-09-01T12:07:00Z"))

    def tearDown(self):
        self.temp.cleanup()

    def forecast(self, conn, key="one", as_of="2026-09-01T12:01:00Z", p=.7,
                 abstain=False, approved=True):
        evidence = {"source_id": "chainlink_btc_twap60",
                    "published_at": "2026-09-01T12:00:59Z", "first_seen_at": "2026-09-01T12:01:00Z",
                    "summary": "illustrative same-feed diffusion proxy P(Up) 0.6000 over 4m"}
        payload = {"market_id": "1", "category": "crypto5m", "as_of": as_of,
                   "end_at": self.market["end_at"], "evidence": [evidence]}
        result = {"market_id": "1", "as_of": as_of, "p_yes": p, "abstain": abstain,
                  "analysis_abstain": abstain, "model_version": "test-model", "prompt_version": "test-v1",
                  "reviewed_at": "2026-09-01T12:03:00Z", "review": {"approved": approved}}
        save_analysis_draft(conn, "1", "rules", key, payload, result)
        final = {**result, "abstain": abstain or not approved}
        pred = save_prediction(conn, "1", "rules", key, final)
        save_analysis_market_baseline(conn, "1", "rules", key, as_of, as_of, as_of, .4, .6)
        save_market_baseline(conn, pred, as_of, as_of, .4, .6)
        return pred

    def test_scores_frozen_same_feed_without_mutating_orders_or_validation(self):
        with database(self.path) as conn:
            self.forecast(conn, approved=False)
            changes = conn.total_changes
            report = build_report(conn, self.cutoff)
            paired = report["overall"]["common_market_relay"]
            self.assertEqual(paired["events"], 1)
            self.assertEqual(paired["metrics"]["model"]["brier"], .09)
            self.assertEqual(paired["metrics"]["relay"]["brier"], .16)
            self.assertEqual(paired["metrics"]["market"]["brier"], .25)
            version = report["by_model_and_prompt"]["test-model"]["test-v1"]
            self.assertEqual(version["rejected"]["market_pair"]["events"], 1)
            self.assertEqual(version["approved"]["market_pair"]["events"], 0)
            self.assertEqual(conn.total_changes, changes)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 0)

    def test_later_original_abstention_is_not_replaced_with_earlier_good_prediction(self):
        with database(self.path) as conn:
            self.forecast(conn)
            selected = self.forecast(conn, key="later", as_of="2026-09-01T12:02:00Z", abstain=True)
            result = build_report(conn, self.cutoff)
            self.assertEqual(result["events"][0]["prediction_id"], selected)
            self.assertEqual(result["overall"]["original_abstentions"], 1)
            self.assertEqual(result["overall"]["market_pair"]["events"], 0)

    def test_actual_utc_sort_and_one_event_selection(self):
        with database(self.path) as conn:
            later = self.forecast(conn, key="later", as_of="2026-09-01T12:02:00Z", p=.8)
            self.forecast(conn, key="offset", as_of="2026-09-01T20:01:00+08:00", p=.2)
            result = build_report(conn, self.cutoff)
            self.assertEqual(result["overall"]["selected_events"], 1)
            self.assertEqual(result["events"][0]["prediction_id"], later)

    def test_future_or_stale_proxy_and_mutated_bid_ask_are_not_paired(self):
        cases = [({"first_seen_at": "2026-09-01T12:01:01Z"},),
                 ({"published_at": "2026-09-01T12:01:01Z"},),
                 ({"published_at": "2026-09-01T12:00:20Z"},)]
        for (update,) in cases:
            with self.subTest(update=update), database(self.path) as conn:
                conn.execute("DELETE FROM market_baselines")
                conn.execute("DELETE FROM predictions")
                conn.execute("DELETE FROM analysis_drafts")
                conn.execute("DELETE FROM analysis_market_baselines")
                self.forecast(conn)
                payload = json.loads(conn.execute("SELECT payload_json FROM analysis_drafts").fetchone()[0])
                payload["evidence"][0].update(update)
                conn.execute("UPDATE analysis_drafts SET payload_json=?", (json.dumps(payload),))
                result = build_report(conn, self.cutoff)
                self.assertIsNone(result["events"][0]["relay"])
                self.assertEqual(result["overall"]["market_pair"]["events"], 1)
        with database(self.path) as conn:
            conn.execute("UPDATE market_baselines SET yes_best_bid=.3,yes_best_ask=.7")
            self.assertIsNone(build_report(conn, self.cutoff)["events"][0]["market"])

    def test_missing_draft_and_late_review_or_unobserved_resolution_excluded(self):
        with database(self.path) as conn:
            self.forecast(conn)
            self.assertEqual(build_report(conn, datetime(2026, 9, 1, 12, 6, 30,
                                                        tzinfo=timezone.utc))["overall"]["selected_events"], 0)
            original = conn.execute("SELECT model_output_json FROM predictions").fetchone()[0]
            output = json.loads(original)
            output["reviewed_at"] = self.market["end_at"]
            conn.execute("UPDATE predictions SET model_output_json=?", (json.dumps(output),))
            self.assertEqual(build_report(conn, self.cutoff)["overall"]["selected_events"], 0)
            conn.execute("UPDATE predictions SET model_output_json=?", (original,))
            conn.execute("UPDATE analysis_drafts SET result_json=?", (json.dumps({"p_yes": .9}),))
            self.assertEqual(build_report(conn, self.cutoff)["overall"]["selected_events"], 0)
