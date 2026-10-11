from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from polycast.config import Config
from polycast.dashboard import make_server
from polycast.dashboard_state import validation_progress
from polycast.store import database


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(Config(), data_dir=Path(self.temp.name))

    def test_three_independent_events_require_review_decision_and_settlement(self) -> None:
        now = datetime.now(timezone.utc)
        forecast = (now - timedelta(hours=4)).isoformat()
        reviewed = (now - timedelta(hours=3)).isoformat()
        decided = (now - timedelta(hours=2)).isoformat()
        resolved = (now - timedelta(hours=1)).isoformat()
        with database(self.config.database) as conn:
            for index in range(3):
                market_id, event_id = str(index + 1), f"independent-{index + 1}"
                conn.execute("""INSERT INTO markets(id,event_id,event_title,event_slug,question,
                    description,end_at,yes_token,no_token,liquidity,volume_24h,min_order_size,
                    tick_size,fee_rate,discovered_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (market_id, event_id, event_id, event_id, "Will event happen?", "rules",
                     resolved, "100", "200", 10000, 1000, 5, .01, 0, forecast, forecast))
                cursor = conn.execute("""INSERT INTO predictions(market_id,as_of,rules_hash,
                    evidence_hash,probability,abstain,reasoning,assumptions_json,supports_json,
                    counters_json,model_output_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (market_id, forecast, "rules", "evidence", .6, 0, "forecast", "[]", "[]",
                     "[]", json.dumps({"review": {"approved": True}, "reviewed_at": reviewed})))
                conn.execute("""INSERT INTO paper_decisions(market_id,event_id,prediction_id,
                    checked_at,status,reason) VALUES(?,?,?,?,?,?)""",
                    (market_id, event_id, cursor.lastrowid, decided, "skipped",
                     "Best conservative net edge 0.0300 is below threshold"))
                conn.execute("""INSERT INTO resolutions(market_id,winning_side,source_url,
                    resolved_at,recorded_at) VALUES(?,?,?,?,?)""",
                    (market_id, "YES", "https://example.org/official", resolved, resolved))
            self.assertEqual(validation_progress(conn)["completed"], 3)
            conn.execute("UPDATE paper_decisions SET reason=? WHERE event_id=?",
                         ("Market data unavailable: HTTP 403", "independent-1"))
            self.assertEqual(validation_progress(conn)["completed"], 2)
            conn.execute("UPDATE paper_decisions SET reason=? WHERE event_id=?",
                         ("Best conservative net edge 0.0300 is below threshold", "independent-1"))
            conn.execute("UPDATE paper_decisions SET checked_at=? WHERE event_id=?",
                         (now.isoformat(), "independent-3"))
            self.assertEqual(validation_progress(conn)["completed"], 2)
            conn.execute("DELETE FROM paper_decisions WHERE event_id=?", ("independent-2",))
            self.assertEqual(validation_progress(conn)["completed"], 1)

    def test_http_api_and_static_shell(self) -> None:
        server = make_server(self.config, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with patch("polycast.dashboard_state.scheduled_task", return_value={"available": False}):
            with urlopen(base + "/api/state") as response:
                state = json.load(response)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(state["validation"]["completed"], 0)
                self.assertEqual(state["mode"], "paper-only")
        with urlopen(base) as response:
            page = response.read().decode()
            self.assertIn('script src="/app.js"', page)
            self.assertNotIn('http-equiv="refresh"', page)
        with urlopen(base + "/app.js") as response:
            script = response.read().decode()
            self.assertIn("setInterval(refresh,15000)", script)
            self.assertIn("立即进行审计预测", script)
            self.assertNotIn("innerHTML", script)
        with patch("polycast.dashboard.live_quote", return_value={"market_id": "123", "quotes": []}) as quote:
            with urlopen(base + "/api/quote?market_id=123") as response:
                self.assertEqual(json.load(response)["market_id"], "123")
            quote.assert_called_once_with(self.config, "123")
        with self.assertRaises(HTTPError) as invalid:
            urlopen(base + "/api/quote?market_id=not-a-number")
        self.assertEqual(invalid.exception.code, 400)

    def test_manual_audit_requires_local_token_and_reports_result(self) -> None:
        end = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with database(self.config.database) as conn:
            conn.execute("""INSERT INTO markets(id,event_id,event_title,event_slug,question,
                description,end_at,yes_token,no_token,liquidity,volume_24h,min_order_size,
                tick_size,fee_rate,discovered_at,updated_at,category)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("123", "event-123", "BTC hourly", "btc-hourly", "Up or Down?", "rules",
                 end, "100", "200", 10000, 1000, 5, .01, 0, end, end, "crypto"))
        server = make_server(self.config, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with patch("polycast.dashboard_state.scheduled_task", return_value={"available": False}):
            token = json.load(urlopen(base + "/api/state"))["scheduler_token"]
        def request(market_id: str, use_token: str = token) -> Request:
            body = json.dumps({"market_id": market_id, "token": use_token}).encode()
            return Request(base + "/api/analyze", data=body,
                           headers={"Content-Type": "application/json", "Origin": base},
                           method="POST")
        with self.assertRaises(HTTPError) as invalid_token:
            urlopen(request("123", "wrong"))
        self.assertEqual(invalid_token.exception.code, 403)
        with self.assertRaises(HTTPError) as missing_market:
            urlopen(request("999"))
        self.assertEqual(missing_market.exception.code, 400)
        with patch("polycast.short_cycle.collect_short") as collect, \
                patch("polycast.cli.analyze", return_value={"status": "analyzed",
                    "prediction_id": 7, "abstain": False, "review_approved": True}) as analyze:
            with urlopen(request("123")) as response:
                self.assertEqual(json.load(response)["job"]["status"], "queued")
            for _ in range(50):
                with patch("polycast.dashboard_state.scheduled_task", return_value={"available": False}):
                    state = json.load(urlopen(base + "/api/state"))
                job = next(x for x in state["analysis_jobs"] if x["market_id"] == "123")
                if job["status"] == "done":
                    break
                time.sleep(.02)
            self.assertEqual(job["status"], "done")
            self.assertTrue(job["review_approved"])
            collect.assert_called_once()
            analyze.assert_called_once_with(self.config, "123")

    def test_task_control_requires_same_origin_and_session_token(self) -> None:
        server = make_server(self.config, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with patch("polycast.dashboard_state.scheduled_task", return_value={"available": True}):
            token = json.load(urlopen(base + "/api/state"))["scheduler_token"]
        payload = json.dumps({"action": "disable", "token": token}).encode()
        def request(origin: str | None, body: bytes) -> Request:
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            return Request(base + "/api/scheduler", data=body, headers=headers, method="POST")
        with patch("polycast.dashboard.manage_task") as control, \
                patch("polycast.dashboard.scheduled_task", return_value={"available": True}):
            with self.assertRaises(HTTPError) as cross_site:
                urlopen(request("https://example.org", payload))
            self.assertEqual(cross_site.exception.code, 403)
            with self.assertRaises(HTTPError) as invalid_token:
                urlopen(request(base, b'{"action":"disable","token":"wrong"}'))
            self.assertEqual(invalid_token.exception.code, 403)
            with urlopen(request(base, payload)) as response:
                self.assertTrue(json.load(response)["scheduler"]["available"])
            control.assert_called_once_with("disable", None)

    def test_settings_control_updates_daily_limit_and_persists(self) -> None:
        server = make_server(self.config, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with patch("polycast.dashboard_state.scheduled_task", return_value={"available": True}):
            token = json.load(urlopen(base + "/api/state"))["scheduler_token"]
        payload = json.dumps({"provider": "deepseek", "daily_limit": 50, "token": token}).encode()
        headers = {"Content-Type": "application/json", "Origin": base}
        req = Request(base + "/api/settings", data=payload, headers=headers, method="POST")
        with urlopen(req) as response:
            self.assertEqual(response.status, 200)
            data = json.load(response)
            self.assertEqual(data["settings"]["max_deepseek_calls_per_utc_day"], 50)
        self.assertEqual(self.config.model_call_limit("deepseek"), 50)

    def test_orders_in_snapshot_includes_settlement(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with database(self.config.database) as conn:
            conn.execute("""INSERT INTO markets(id,event_id,event_title,event_slug,question,
                description,end_at,yes_token,no_token,liquidity,volume_24h,min_order_size,
                tick_size,fee_rate,discovered_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("999", "event-999", "E999", "e999", "Will BTC go up?", "rules",
                 now, "100", "200", 10000, 1000, 5, .01, 0, now, now))
            pred = conn.execute("""INSERT INTO predictions(market_id,as_of,rules_hash,
                evidence_hash,probability,abstain,reasoning,assumptions_json,supports_json,
                counters_json,model_output_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("999", now, "r", "e", .5, 0, "test", "[]", "[]", "[]", "{}")).lastrowid
            conn.execute("""INSERT INTO paper_orders(prediction_id,market_id,event_id,risk_group,
                side,created_at,shares,avg_price,fee,total_cost,expected_edge,status,settlement)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pred, "999", "event-999", "btc-5m", "NO", now, 5.0, 0.04, 0.01, 0.21, 0.1, "settled", 0.0))
        from polycast.dashboard_state import snapshot
        state = snapshot(self.config)
        matching = [o for o in state["orders"] if o["id"] == 1]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["settlement"], 0.0)


if __name__ == "__main__":
    unittest.main()
