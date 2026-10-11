from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polycast.evaluation import report
from polycast.store import (database, save_analysis_draft, save_analysis_market_baseline,
                            save_market_baseline, save_prediction, upsert_markets)


class ShadowReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.market = {"id": "1", "event_id": "event-1", "event_title": "Bitcoin 5m",
                       "event_slug": "btc-updown-5m-1", "question": "Bitcoin up?",
                       "description": "TWAP rules", "end_at": "2026-09-01T12:05:00Z",
                       "yes_token": "up", "no_token": "down", "liquidity": 1000,
                       "volume_24h": 1000, "min_order_size": 5, "tick_size": .01,
                       "fee_rate": 0, "category": "crypto5m", "yes_label": "Up",
                       "no_label": "Down", "source_ref": "BTCUSD:1"}
        with database(self.path) as conn:
            upsert_markets(conn, [self.market])
            conn.execute("""INSERT INTO resolutions VALUES(?,?,?,?,?)""",
                         ("1", "YES", "https://gamma-api.polymarket.com/events/slug/test",
                          "2026-09-01T12:06:00Z", "2026-09-01T12:07:00Z"))

    def tearDown(self):
        self.temp.cleanup()

    def forecast(self, conn, *, key="a", as_of="2026-09-01T12:01:00Z",
                 reviewed_at="2026-09-01T12:01:20Z", p=.7,
                 analysis_abstain=False, legacy=False, draft_abstain=False,
                 draft_p=None, draft_market="1", model="deepseek-v4-pro"):
        result = {"market_id": "1", "as_of": as_of, "p_yes": p, "abstain": True,
                  "review": {"approved": False}, "reviewed_at": reviewed_at,
                  "prompt_version": "prompt-test", "model_version": model}
        if not legacy:
            result["analysis_abstain"] = analysis_abstain
        prediction_id = save_prediction(conn, "1", "rules", key, result)
        if legacy:
            save_analysis_draft(conn, "1", "rules", key, {"as_of": as_of},
                                {"market_id": draft_market, "as_of": as_of,
                                 "abstain": draft_abstain,
                                 "p_yes": p if draft_p is None else draft_p})
        save_analysis_market_baseline(conn, "1", "rules", key, as_of, as_of, as_of, .4, .6)
        save_market_baseline(conn, prediction_id, as_of, as_of, .4, .6)
        return prediction_id

    def test_rejection_scored_without_validation_credit_or_orders(self):
        with database(self.path) as conn:
            self.forecast(conn)
            scored = report(conn)
            shadow = scored["rejected_five_minute_shadow"]
            self.assertEqual((shadow["events"], shadow["paired_events"]), (1, 1))
            self.assertEqual(shadow["paired_model_brier"], .09)
            self.assertEqual(shadow["paired_market_brier"], .25)
            self.assertEqual(shadow["by_model_and_prompt"]["deepseek-v4-pro"]["prompt-test"]["events"], 1)
            self.assertEqual(scored["complete_scored_events"], 0)
            self.assertEqual(scored["reviewed_pre_outcome_markets"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_decisions").fetchone()[0], 0)

    def test_legacy_flag_requires_exact_frozen_analysis(self):
        with database(self.path) as conn:
            self.forecast(conn, legacy=True)
            shadow = report(conn)["rejected_five_minute_shadow"]
            self.assertEqual(shadow["events"], 1)
            self.assertEqual(shadow["markets"][0]["analysis_flag_source"], "frozen_analysis_draft")
            conn.execute("UPDATE analysis_drafts SET result_json=?",
                         (json.dumps({"market_id": "other", "as_of": "2026-09-01T12:01:00Z",
                                      "p_yes": .7, "abstain": False}),))
            self.assertEqual(report(conn)["rejected_five_minute_shadow"]["events"], 0)

    def test_true_abstain_invalid_probability_and_late_review_excluded(self):
        cases = [{"analysis_abstain": True}, {"legacy": True, "draft_abstain": True},
                 {"legacy": True, "draft_p": .8}, {"p": float("inf")}, {"p": True},
                 {"p": 1.1}, {"reviewed_at": "2026-09-01T12:06:01Z"},
                 {"reviewed_at": "2026-09-01T12:05:00Z"}]
        for i, args in enumerate(cases):
            with self.subTest(args=args), database(self.path) as conn:
                self.forecast(conn, key=str(i), **args)
                self.assertEqual(report(conn)["rejected_five_minute_shadow"]["events"], 0)

    def test_actual_utc_order_selects_one_latest_prediction(self):
        with database(self.path) as conn:
            later = self.forecast(conn, key="late", as_of="2026-09-01T12:02:00Z", p=.8,
                                  reviewed_at="2026-09-01T12:02:20Z")
            self.forecast(conn, key="early", as_of="2026-09-01T20:01:00+08:00", p=.2,
                          reviewed_at="2026-09-01T12:01:20Z", model="deepseek-flash")
            shadow = report(conn)["rejected_five_minute_shadow"]
            self.assertEqual(shadow["events"], 1)
            self.assertEqual(shadow["markets"][0]["prediction_id"], later)
            self.assertEqual(shadow["model_brier"], .04)
            self.assertNotIn("deepseek-flash", shadow["by_model"])

    def test_unexpected_multi_market_event_not_double_counted(self):
        with database(self.path) as conn:
            self.forecast(conn)
            upsert_markets(conn, [{**self.market, "id": "2"}], reset_watchlist=False)
            shadow = report(conn)["rejected_five_minute_shadow"]
            self.assertEqual(shadow["events"], 0)
            self.assertEqual(shadow["excluded_multi_market_events"], 1)

    def test_approved_report_also_uses_actual_utc_order(self):
        with database(self.path) as conn:
            later = self.forecast(conn, key="late", as_of="2026-09-01T12:02:00Z", p=.8,
                                  reviewed_at="2026-09-01T12:02:20Z")
            self.forecast(conn, key="early", as_of="2026-09-01T20:01:00+08:00", p=.2,
                          reviewed_at="2026-09-01T12:01:20Z")
            for row in conn.execute("SELECT id,model_output_json FROM predictions").fetchall():
                output = json.loads(row["model_output_json"])
                output["review"]["approved"] = True
                output["abstain"] = False
                conn.execute("UPDATE predictions SET model_output_json=?,abstain=0,probability=? WHERE id=?",
                             (json.dumps(output), output["p_yes"], row["id"]))
            scored = report(conn)
            self.assertEqual(scored["markets"][0]["prediction_id"], later)
            self.assertEqual(scored["markets"][0]["brier"], .04)

    def test_invalid_or_nonidentical_pre_model_book_not_paired(self):
        updates = ["yes_best_bid=.3,yes_best_ask=.7", "p_yes_mid=1.2",
                   "p_yes_mid=0.6", "yes_best_bid=1e999",
                   "captured_at='2026-09-01T12:01:01Z'",
                   "captured_at='2026-09-01T12:00:49Z'",
                   "book_timestamp='2026-09-01T12:00:49Z'"]
        for update in updates:
            with self.subTest(update=update), database(self.path) as conn:
                conn.execute("DELETE FROM market_baselines")
                conn.execute("DELETE FROM analysis_market_baselines")
                conn.execute("DELETE FROM predictions")
                self.forecast(conn)
                conn.execute("UPDATE market_baselines SET " + update)
                shadow = report(conn)["rejected_five_minute_shadow"]
                self.assertEqual(shadow["events"], 1)
                self.assertEqual(shadow["paired_events"], 0)


if __name__ == "__main__":
    unittest.main()
