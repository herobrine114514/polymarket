from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from polycast.cli import analyze
from polycast.config import Config
from polycast.store import database, save_prediction, upsert_markets


class FiveMinuteMutexTests(unittest.TestCase):
    def test_concurrent_analysis_does_not_spend_calls_and_auto_rechecks_under_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(data_dir=Path(directory))
            market = {"id": "1", "event_id": "e", "event_title": "BTC 5m",
                      "event_slug": "btc", "question": "Up?", "description": "rules",
                      "end_at": "2027-01-01T00:00:00Z", "yes_token": "up", "no_token": "down",
                      "liquidity": 10000, "volume_24h": 1000, "min_order_size": 5,
                      "tick_size": .01, "fee_rate": 0, "category": "crypto5m"}
            with database(config.database) as conn:
                upsert_markets(conn, [market])
            entered, release = threading.Event(), threading.Event()
            errors, outcomes = [], []

            def model_pipeline(*args):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release analysis")
                with database(config.database) as conn:
                    count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
                    pred = save_prediction(conn, "1", "rules" + str(count), "evidence" + str(count), {
                        "as_of": "2026-09-30T07:31:00Z", "p_yes": .6, "abstain": False,
                        "reviewed_at": "2026-09-30T07:31:10Z", "review": {"approved": True}})
                return {"status": "analyzed", "prediction_id": pred}

            def first_caller():
                try:
                    outcomes.append(analyze(config, "1", once_per_window=True))
                except Exception as exc:
                    errors.append(exc)

            with patch("polycast.cli._analyze_unlocked", side_effect=model_pipeline) as pipeline:
                thread = threading.Thread(target=first_caller)
                thread.start()
                try:
                    self.assertTrue(entered.wait(5))
                    with self.assertRaisesRegex(RuntimeError, "already running"):
                        analyze(config, "1", once_per_window=True)
                    self.assertEqual(pipeline.call_count, 1)
                finally:
                    release.set()
                    thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors)
                self.assertEqual(outcomes[0]["status"], "analyzed")
                self.assertEqual(analyze(config, "1", once_per_window=True)["status"], "already_analyzed")
                self.assertEqual(pipeline.call_count, 1)
                # Explicit manual review may reevaluate the same market after
                # the previous request finishes; it still uses the same lock.
                self.assertEqual(analyze(config, "1")["status"], "analyzed")
                self.assertEqual(pipeline.call_count, 2)
