from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polycast.provider import active_provider, select_provider
from polycast.paper import _prediction_current
from polycast.config import Config
from polycast.auto5m import AutoFiveMinute, _market_claim_status
from polycast.short_cycle import _twap_snapshot, _twap_winner, reconcile_short
from polycast.store import (database, finish_model_call, model_calls_today,
                            model_pause_until, reserve_model_call)


class ProviderIsolationTests(unittest.TestCase):
    def test_deepseek_switch_ignores_codex_pause_and_has_own_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict("os.environ", {"POLYCAST_DEEPSEEK_API_KEY": "sk-test-only-value"}):
                self.assertEqual(active_provider(root), "codex")
                select_provider(root, "deepseek")
                self.assertEqual(active_provider(root), "deepseek")
            with database(root / "db.sqlite3") as conn:
                instant = datetime.now(timezone.utc).isoformat()
                conn.execute("""INSERT INTO markets(id,event_id,event_title,event_slug,question,
                    description,end_at,yes_token,no_token,liquidity,volume_24h,min_order_size,
                    tick_size,fee_rate,discovered_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("1", "event-1", "Event", "event", "Question", "Rules", instant,
                     "10", "11", 10000, 1000, 5, .01, 0, instant, instant))
                codex = reserve_model_call(conn, "1", "analysis", "gpt-6-astra", 1)
                finish_model_call(conn, codex, "usage limit", "2099-01-01T00:00:00+00:00")
                self.assertEqual(model_calls_today(conn, "codex"), 1)
                self.assertIsNotNone(model_pause_until(conn, "codex"))
                self.assertIsNone(model_pause_until(conn, "deepseek"))
                deepseek = reserve_model_call(conn, "1", "analysis", "deepseek-flash", 1,
                                              "deepseek")
                finish_model_call(conn, deepseek, provider="deepseek")
                self.assertEqual(model_calls_today(conn, "deepseek"), 1)
                pro = reserve_model_call(conn, "1", "review", "deepseek-v4-pro", 2,
                                         "deepseek")
                finish_model_call(conn, pro, provider="deepseek")
                self.assertEqual(model_calls_today(conn, "deepseek"), 2)
                with self.assertRaisesRegex(RuntimeError, "Daily Deepseek call limit"):
                    reserve_model_call(conn, "1", "analysis", "deepseek-v4-pro", 2,
                                       "deepseek")


class ChainlinkTwapTests(unittest.TestCase):
    def test_worker_does_not_replace_scheduled_forecast_in_same_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with database(Path(directory) / "db.sqlite3") as conn:
                instant = datetime.now(timezone.utc).isoformat()
                conn.execute("""INSERT INTO markets(id,event_id,event_title,event_slug,question,
                    description,end_at,yes_token,no_token,liquidity,volume_24h,min_order_size,
                    tick_size,fee_rate,discovered_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("123", "event-123", "BTC 5m", "btc-updown-5m-123", "Up?", "rules",
                     instant, "10", "11", 10000, 1000, 5, .01, 0, instant, instant))
                self.assertIsNone(_market_claim_status(conn, "123"))
                conn.execute("""INSERT INTO model_runs(market_id,stage,model_version,started_at,status)
                    VALUES(?,?,?,?,?)""", ("123", "analysis", "deepseek-v4-pro", instant, "running"))
                self.assertEqual(_market_claim_status(conn, "123"), "running")
                conn.execute("""INSERT INTO predictions(market_id,as_of,rules_hash,evidence_hash,
                    probability,abstain,reasoning,assumptions_json,supports_json,counters_json,
                    model_output_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ("123", instant, "rules", "evidence", .4, 0, "forecast", "[]", "[]", "[]", "{}"))
                self.assertEqual(_market_claim_status(conn, "123"), "predicted")

    def test_collector_uses_windows_proxy_for_node_websocket(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
                "polycast.collector_env.getproxies", return_value={"https": "http://127.0.0.1:7897"}), patch(
                "polycast.auto5m.shutil.which", return_value="node"), patch(
                "polycast.auto5m.subprocess.Popen") as popen, patch.dict(
                "os.environ", {}, clear=True):
            AutoFiveMinute(Config(data_dir=Path(directory)))._ensure_collector()
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7897")
            self.assertEqual(env["NODE_USE_ENV_PROXY"], "1")
            self.assertEqual(env["POLYCAST_PARENT_PIPE"], "1")
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)

    def test_worker_reports_stale_chainlink_data_even_when_collector_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = AutoFiveMinute(Config(data_dir=root))
            worker._started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            worker._collector = type("Collector", (), {"poll": lambda self: None})()
            stale = datetime.now(timezone.utc) - timedelta(minutes=2)
            (root / "chainlink_twap60.jsonl").write_text(json.dumps({
                "timestamp_ms": int(stale.timestamp() * 1000),
                "full_accuracy_value": "83763475604562545475584"}) + "\n",
                encoding="utf-8")
            state = worker.state()
            self.assertEqual(state["status"], "degraded")
            self.assertFalse(state["collector_fresh"])
            self.assertGreaterEqual(state["collector_data_age_seconds"], 119)
            fresh = datetime.now(timezone.utc)
            (root / "chainlink_twap60.jsonl").write_text(json.dumps({
                "timestamp_ms": int(fresh.timestamp() * 1000),
                "full_accuracy_value": "83763475604562545475584"}) + "\n",
                encoding="utf-8")
            self.assertTrue(worker.state()["collector_fresh"])

    def test_hourly_prediction_requires_current_binance_minute(self) -> None:
        now = datetime.now(timezone.utc)
        market = {"category": "crypto"}
        evidence = [{"source_id": "binance_klines",
                     "updated_at": (now - timedelta(minutes=3)).isoformat()}]
        prediction = {"rules_hash": "rules", "evidence_hash": "evidence"}
        with tempfile.TemporaryDirectory() as directory, patch(
                "polycast.paper.evidence_for_market", return_value=evidence), patch(
                "polycast.paper.make_payload", return_value={}), patch(
                "polycast.paper.hashes", return_value=("rules", "evidence")):
            config = Config(data_dir=Path(directory))
            self.assertFalse(_prediction_current(None, market, prediction, config))
            evidence[0]["updated_at"] = (now - timedelta(seconds=40)).isoformat()
            self.assertTrue(_prediction_current(None, market, prediction, config))

    def test_recently_closed_five_minute_market_is_reconciled_without_thirty_minute_delay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            end = now - timedelta(minutes=3)
            start = end - timedelta(minutes=5)
            with database(Path(directory) / "db.sqlite3") as conn:
                conn.execute("""INSERT INTO markets(id,event_id,event_title,event_slug,question,
                    description,end_at,yes_token,no_token,liquidity,volume_24h,min_order_size,
                    tick_size,fee_rate,category,source_ref,discovered_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("123", "event-123", "BTC 5m", "btc-updown-5m-123", "Up?", "rules",
                     end.isoformat(), "10", "11", 10000, 1000, 5, .01, 0,
                     "crypto5m", f"BTCUSD:{int(start.timestamp())}",
                     now.isoformat(), now.isoformat()))
                with patch("polycast.short_cycle.fetch_json", return_value={"id": "event-123", "closed": False,
                            "markets": [{"id": "123"}]}):
                    result = reconcile_short(conn, categories=("crypto5m",))
                self.assertEqual(result["pending"], ["123"])

    def test_five_minute_prediction_expires_before_delayed_quote(self) -> None:
        now = datetime.now(timezone.utc)
        market = {"category": "crypto5m"}
        relay = {"source_id": "chainlink_btc_twap60",
                 "updated_at": (now - timedelta(seconds=2)).isoformat()}
        with tempfile.TemporaryDirectory() as directory, patch(
                "polycast.paper.evidence_for_market", return_value=[relay]), patch(
                "polycast.paper.make_payload", return_value={}), patch(
                "polycast.paper.hashes", return_value=("rules", "evidence")):
            config = Config(data_dir=Path(directory))
            old = {"model_output_json": json.dumps({
                "reviewed_at": (now - timedelta(seconds=106)).isoformat()}),
                "rules_hash": "rules", "evidence_hash": "evidence"}
            self.assertFalse(_prediction_current(None, market, old, config))
            recent = {**old, "model_output_json": json.dumps({
                "reviewed_at": (now - timedelta(seconds=2)).isoformat()})}
            self.assertTrue(_prediction_current(None, market, recent, config))

    def test_live_snapshot_requires_near_open_and_fresh_relay_tick(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.jsonl"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            start = now - timedelta(minutes=2)
            market = {"id": "123", "category": "crypto5m",
                      "source_ref": f"BTCUSD:{int(start.timestamp())}",
                      "end_at": (start + timedelta(minutes=5)).isoformat()}
            ticks = [(start, 85000), (start + timedelta(seconds=1), 85001),
                     (now - timedelta(seconds=3), 85020)]
            path.write_text("".join(json.dumps({"timestamp_ms": int(t.timestamp() * 1000),
                              "received_at_ms": int(t.timestamp() * 1000) + 500,
                              "full_accuracy_value": str(price * 10**18)}) + "\n"
                              for t, price in ticks), encoding="utf-8")
            snapshot = _twap_snapshot(market, now, path)
            self.assertIsNotNone(snapshot)
            self.assertIn("85000", snapshot["summary"])
            self.assertEqual(snapshot["source_id"], "chainlink_btc_twap60")
            self.assertIsNone(_twap_snapshot(market, now + timedelta(seconds=40), path))

    def test_live_snapshot_uses_same_feed_history_for_volatility_scale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.jsonl"
            now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            start = now - timedelta(minutes=2)
            market = {"id": "124", "category": "crypto5m",
                      "source_ref": f"BTCUSD:{int(start.timestamp())}",
                      "end_at": (start + timedelta(minutes=5)).isoformat()}
            prices = [(start - timedelta(minutes=i), 85000 + (i % 3) * 25)
                      for i in range(15, -1, -1)]
            prices += [(start + timedelta(seconds=1), 85005),
                       (now - timedelta(seconds=3), 85100)]
            path.write_text("".join(json.dumps({
                "timestamp_ms": int(t.timestamp() * 1000),
                "received_at_ms": int(t.timestamp() * 1000) + 500,
                "full_accuracy_value": str(price * 10**18)}) + "\n"
                for t, price in prices), encoding="utf-8")
            snapshot = _twap_snapshot(market, now, path)
            self.assertIsNotNone(snapshot)
            self.assertIn("consecutive 1m RTDS TWAP log returns", snapshot["summary"])
            self.assertIn("same-feed diffusion proxy P(Up)", snapshot["summary"])
            self.assertIn("Opening sample offset +0s", snapshot["summary"])

    def test_settlement_requires_boundary_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.jsonl"
            end = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=3)
            start = end - timedelta(minutes=5)
            market = {"source_ref": f"BTCUSD:{int(start.timestamp())}",
                      "end_at": end.isoformat()}
            path.write_text("".join(json.dumps({"timestamp_ms": int(t.timestamp() * 1000),
                              "received_at_ms": int(t.timestamp() * 1000) + 500,
                              "full_accuracy_value": str(price * 10**18)}) + "\n"
                              for t, price in [(start, 85000), (end, 85010)]), encoding="utf-8")
            self.assertEqual(_twap_winner(market, path), "YES")
            path.write_text(json.dumps({"timestamp_ms": int(start.timestamp() * 1000),
                                        "full_accuracy_value": str(85000 * 10**18)}) + "\n",
                            encoding="utf-8")
            self.assertIsNone(_twap_winner(market, path))
