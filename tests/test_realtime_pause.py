from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from polycast.auto5m import AutoFiveMinute
from polycast.cli import analyze, cycle
from polycast.config import Config, save_runtime_settings
from polycast.dashboard import make_server
from polycast.store import database, upsert_markets


class RealtimePauseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(data_dir=Path(self.temp.name))
        start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=100)
        self.market = {"id": "1", "event_id": "btc-event", "event_title": "Bitcoin Up or Down",
                       "event_slug": "btc-updown-5m-" + str(int(start.timestamp())),
                       "question": "Bitcoin Up or Down", "description": "Chainlink TWAP 60s",
                       "source_ref": "BTCUSD:" + str(int(start.timestamp())), "category": "crypto5m",
                       "end_at": (start + timedelta(minutes=5)).isoformat(),
                       "yes_token": "up", "no_token": "down", "liquidity": 10000,
                       "volume_24h": 1000, "min_order_size": 5, "tick_size": .01, "fee_rate": .07}
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])

    def test_paused_worker_keeps_collection_and_reconciliation_without_models_or_orders(self):
        save_runtime_settings(self.config.data_dir, {"btc5m_auto_enabled": False})
        worker = AutoFiveMinute(self.config)
        with patch.object(worker, "_ensure_collector") as collector, patch.object(
                worker._stop, "wait", side_effect=lambda _s: worker._stop.set()), patch(
                "polycast.auto5m.discover_btc_5m", return_value=[self.market]), patch(
                "polycast.auto5m.reconcile_short") as reconcile, patch(
                "polycast.cli.analyze") as analyze, patch("polycast.cli.paper") as paper:
            worker._loop()
        collector.assert_called_once()
        reconcile.assert_called_once()
        analyze.assert_not_called()
        paper.assert_not_called()
        self.assertEqual(worker.state()["status"], "paused")

    def test_pause_during_analysis_prevents_its_automatic_paper_entry(self):
        worker = AutoFiveMinute(self.config)
        def analysis(*_args, **_kwargs):
            save_runtime_settings(self.config.data_dir, {"btc5m_auto_enabled": False})
            return {"status": "analyzed"}
        with patch.object(worker, "_ensure_collector"), patch.object(
                worker._stop, "wait", side_effect=lambda _s: worker._stop.set()), patch(
                "polycast.auto5m.discover_btc_5m", return_value=[self.market]), patch(
                "polycast.auto5m.collect_short"), patch("polycast.auto5m.evidence_for_market",
                return_value=[{"source_id": "chainlink_btc_twap60"}]), patch(
                "polycast.auto5m.reconcile_short"), patch("polycast.cli.analyze", side_effect=analysis), patch(
                "polycast.cli.paper") as paper:
            worker._loop()
        paper.assert_not_called()
        self.assertEqual(worker.state()["last_action"], "paused")

    def test_scheduled_cycle_respects_same_five_minute_pause(self):
        save_runtime_settings(self.config.data_dir, {"btc5m_auto_enabled": False})
        with patch("polycast.cli.scan", return_value=[self.market]), patch(
                "polycast.cli.collect", return_value={"fetched": 1, "errors": []}) as collect, patch(
                "polycast.cli.reconcile_short", return_value={}) as reconcile, patch(
                "polycast.cli.refresh_calendars", return_value={}), patch(
                "polycast.cli.analyze") as analyze, patch("polycast.cli.paper") as paper:
            result = cycle(self.config)
        collect.assert_called_once()
        reconcile.assert_called_once()
        analyze.assert_not_called()
        paper.assert_not_called()
        self.assertEqual(result["analyses"], [])

    def test_analysis_lock_enforces_automatic_pause_but_manual_audit_remains_available(self):
        save_runtime_settings(self.config.data_dir, {"btc5m_auto_enabled": False})
        with patch("polycast.cli._analyze_unlocked", return_value={"status": "analyzed"}) as pipeline:
            self.assertEqual(analyze(self.config, "1", once_per_window=True)["status"], "paused")
            pipeline.assert_not_called()
            self.assertEqual(analyze(self.config, "1")["status"], "analyzed")
            pipeline.assert_called_once()

    def test_pause_during_scheduled_analysis_also_prevents_automatic_entry(self):
        def prediction(*_args, **_kwargs):
            save_runtime_settings(self.config.data_dir, {"btc5m_auto_enabled": False})
            return {"status": "analyzed"}
        now = datetime.now(timezone.utc).isoformat()
        with patch("polycast.cli.scan", return_value=[self.market]), patch(
                "polycast.cli.collect", return_value={"fetched": 1, "errors": []}), patch(
                "polycast.cli.reconcile_short", return_value={}), patch(
                "polycast.cli.refresh_calendars", return_value={}), patch(
                "polycast.cli.evidence_for_market", return_value=[{
                    "id": 1, "source_id": "chainlink_btc_twap60", "updated_at": now}]), patch(
                "polycast.cli.make_payload", return_value={}), patch(
                "polycast.cli.hashes", return_value=("rules", "evidence")), patch(
                "polycast.cli.analyze", side_effect=prediction), patch("polycast.cli.paper") as paper:
            result = cycle(self.config)
        paper.assert_not_called()
        self.assertEqual(result["analyses"][0]["paper"]["status"], "paused")

    def test_local_control_persists_pause_and_resume_without_changing_windows_tasks(self):
        server = make_server(self.config, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch("polycast.dashboard_state.scheduled_task", return_value={"available": True}):
                with urlopen(base + "/api/state") as response:
                    token = json.load(response)["scheduler_token"]
            with patch("polycast.dashboard.manage_task") as windows, patch(
                    "polycast.dashboard.scheduled_task", return_value={"available": True}):
                for action, enabled in (("pause_predictions", False), ("resume_predictions", True)):
                    body = json.dumps({"target": "realtime", "action": action, "token": token}).encode()
                    request = Request(base + "/api/scheduler", data=body, method="POST",
                                      headers={"Content-Type": "application/json", "Origin": base})
                    with urlopen(request) as response:
                        self.assertEqual(json.load(response)["settings"]["btc5m_auto_enabled"], enabled)
                    self.assertEqual(self.config.five_minute_auto_enabled(), enabled)
                bad = Request(base + "/api/scheduler", method="POST",
                              data=b'{"target":"realtime","action":"pause_predictions","token":"bad"}',
                              headers={"Content-Type": "application/json", "Origin": base})
                with self.assertRaises(HTTPError) as denied:
                    urlopen(bad)
                self.assertEqual(denied.exception.code, 403)
                windows.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
