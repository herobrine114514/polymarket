from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from polycast.cli import cycle
from polycast.config import Config
from polycast.market import discover
from polycast.runner import run_recorded
from polycast.store import database, upsert_markets


class DiscoveryOutageTests(unittest.TestCase):
    def test_one_sector_outage_does_not_block_other_sectors_and_is_audited(self):
        errors = []
        with patch("polycast.market._discover_macro", side_effect=RuntimeError("TLS EOF")), patch(
                "polycast.market.discover_btc_5m", return_value=[{"id": "btc"}]), patch(
                "polycast.market.discover_hourly_crypto", return_value=[{"id": "hour"}]), patch(
                "polycast.market.discover_daily_mlb", return_value=[{"id": "game"}]):
            self.assertEqual([m["id"] for m in discover(errors=errors)], ["btc", "hour", "game"])
            self.assertEqual(errors, ["macro: RuntimeError: TLS EOF"])
            with self.assertRaisesRegex(RuntimeError, "TLS EOF"):
                discover()  # Un-audited standalone callers retain strict failures.

    def test_total_discovery_outage_preserves_news_and_cached_markets_without_trading(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(Config(data_dir=Path(directory)), max_analyses_per_cycle=0)
            cached = {"id": "1", "event_id": "e", "event_title": "Fed decision",
                      "event_slug": "fed", "question": "Hold?", "description": "rules",
                      "end_at": "2027-01-01T00:00:00Z", "yes_token": "up", "no_token": "down",
                      "liquidity": 10000, "volume_24h": 1000, "min_order_size": 5,
                      "tick_size": .01, "fee_rate": 0}
            with database(config.database) as conn:
                upsert_markets(conn, [cached])
            with patch("polycast.market._discover_macro", side_effect=RuntimeError("macro offline")), patch(
                    "polycast.market.discover_btc_5m", side_effect=RuntimeError("5m offline")), patch(
                    "polycast.market.discover_hourly_crypto", side_effect=RuntimeError("hour offline")), patch(
                    "polycast.market.discover_daily_mlb", side_effect=RuntimeError("daily offline")), patch(
                    "polycast.cli.collect", return_value={"fetched": 5, "new_versions": 2,
                                                         "errors": []}) as collect, patch(
                    "polycast.cli.reconcile_short", return_value={"settled": [], "errors": []}), patch(
                    "polycast.cli.refresh_calendars", return_value={}), patch(
                    "polycast.cli.analyze") as analyze, patch("polycast.cli.paper") as paper:
                result = run_recorded(config, lambda: cycle(config))
            collect.assert_called_once()
            analyze.assert_not_called()
            paper.assert_not_called()
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["summary"]["error_count"], 4)
            self.assertEqual(result["summary"]["evidence_fetched"], 5)
            self.assertEqual(result["result"]["watchlist_markets"], 0)
            with database(config.database) as conn:
                self.assertEqual(conn.execute("SELECT in_watchlist FROM markets WHERE id='1'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT model_calls FROM cycle_runs").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 0)
