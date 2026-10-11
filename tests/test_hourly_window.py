from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polycast.cli import analyze, cycle
from polycast.config import Config
from polycast.short_cycle import _crypto_snapshot, hourly_window_ready
from polycast.store import database, upsert_markets


def market(number, start):
    start = start.replace(microsecond=0)
    return {"id": str(number), "event_id": "event-" + str(number),
            "event_title": "Bitcoin Up or Down", "event_slug": "btc-hour-" + str(number),
            "question": "Bitcoin Up or Down", "description": "Binance BTC/USDT 1 hour candle",
            "end_at": (start + timedelta(hours=1)).isoformat(),
            "source_ref": "BTCUSDT:" + str(int(start.timestamp())), "category": "crypto",
            "yes_token": "up-" + str(number), "no_token": "down-" + str(number),
            "liquidity": 10000, "volume_24h": 1000, "min_order_size": 5,
            "tick_size": .01, "fee_rate": .07}


class HourlyWindowTests(unittest.TestCase):
    def test_cycle_analyzes_current_window_without_treating_future_window_as_outage(self):
        now = datetime.now(timezone.utc)
        active = market(1, now - timedelta(minutes=25))
        future = market(2, now + timedelta(minutes=35))
        with tempfile.TemporaryDirectory() as directory:
            config = Config(data_dir=Path(directory))
            with database(config.database) as conn:
                upsert_markets(conn, [active, future])
            # A news article is relevant to both windows, but cannot make a
            # future opening candle exist. This reproduces the real cycle bug.
            news = [{"id": 1, "source_id": "news", "summary": "BTC announcement",
                     "title": "BTC announcement", "url": "https://example.com/news",
                     "published_at": now.isoformat(), "first_seen_at": now.isoformat()}]
            for selected, expected in (([future], 0), ([active, future], 1)):
                with self.subTest(selected=[m["id"] for m in selected]), patch(
                        "polycast.cli.scan", return_value=selected), patch(
                        "polycast.cli.collect", return_value={"fetched": 1, "errors": []}), patch(
                        "polycast.cli.reconcile_short", return_value={"settled": [], "errors": []}), patch(
                        "polycast.cli.refresh_calendars", return_value={}), patch(
                        "polycast.cli.evidence_for_market", return_value=news), patch(
                        "polycast.cli.make_payload", return_value={}), patch(
                        "polycast.cli.hashes", return_value=("rules", "evidence")), patch(
                        "polycast.cli.analyze", return_value={"status": "analyzed"}) as invoke, patch(
                        "polycast.cli.paper", return_value={"status": "skipped"}) as paper:
                    result = cycle(config)
                self.assertEqual(invoke.call_count, expected)
                self.assertEqual(paper.call_count, expected)
                if expected:
                    invoke.assert_called_once_with(config, "1", once_per_window=False)
                self.assertEqual(len(result["analyses"]), expected)
                self.assertFalse(any(e.get("status") == "error" for e in result["analyses"]))

    def test_manual_future_or_initial_minute_never_fetches_or_calls_model(self):
        now = datetime.now(timezone.utc)
        waiting = [market(1, now + timedelta(minutes=10)),
                   market(2, now - timedelta(seconds=10))]
        with tempfile.TemporaryDirectory() as directory:
            config = Config(data_dir=Path(directory))
            with database(config.database) as conn:
                upsert_markets(conn, waiting)
            with patch("polycast.cli.collect_short") as collect, patch(
                    "polycast.cli.run_codex") as model:
                for m in waiting:
                    with self.subTest(market=m["id"]), self.assertRaisesRegex(ValueError, "first complete minute"):
                        analyze(config, m["id"])
                collect.assert_not_called()
                model.assert_not_called()

    def test_exact_first_completed_minute_boundary_and_snapshot_no_future_fetch(self):
        start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        m = market(1, start)
        self.assertFalse(hourly_window_ready(m, start + timedelta(seconds=59)))
        self.assertTrue(hourly_window_ready(m, start + timedelta(seconds=60)))
        self.assertFalse(hourly_window_ready(m, start + timedelta(hours=1)))
        with patch("polycast.short_cycle.fetch_json") as fetch:
            self.assertIsNone(_crypto_snapshot(m, start - timedelta(seconds=1)))
            self.assertIsNone(_crypto_snapshot(m, start + timedelta(seconds=59)))
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
