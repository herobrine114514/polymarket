from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from polycast.net import fetch_bytes
from polycast.short_cycle import collect_short
from polycast.store import database


class NetworkReadTimeoutTests(unittest.TestCase):
    def test_response_read_failure_is_reported_as_the_network_error_callers_handle(self):
        for error in (TimeoutError("read timed out"), ConnectionResetError("reset")):
            with self.subTest(error=type(error).__name__), patch(
                    "polycast.net.urllib.request.urlopen",
                    return_value=nullcontext(Mock(read=Mock(side_effect=error)))):
                with self.assertRaisesRegex(RuntimeError, type(error).__name__) as failure:
                    fetch_bytes("https://example.com/public-market")
                self.assertIs(failure.exception.__cause__, error)

    def test_one_real_response_read_timeout_does_not_discard_other_market_snapshot(self):
        start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        now = start + timedelta(minutes=30, seconds=20)
        candles = []
        for minute in range(-120, 30):
            opened = int((start + timedelta(minutes=minute)).timestamp() * 1000)
            price = 100 + minute * .01
            candles.append([opened, str(price), "0", "0",
                            str(price + (.02 if minute % 2 else -.01)), "0", opened + 59999])
        markets = [{"id": str(i), "source_ref": "BTCUSDT:" + str(int(start.timestamp())),
                    "end_at": (start + timedelta(hours=1)).isoformat(), "category": "crypto"}
                   for i in (1, 2)]
        responses = [nullcontext(Mock(read=Mock(side_effect=TimeoutError("read timed out")))),
                     nullcontext(Mock(read=Mock(return_value=json.dumps(candles).encode())))]
        with tempfile.TemporaryDirectory() as directory, database(Path(directory) / "test.db") as conn:
            with patch("polycast.short_cycle.datetime", wraps=datetime) as clock, patch(
                    "polycast.net.urllib.request.urlopen", side_effect=responses) as network:
                clock.now.return_value = now
                result = collect_short(conn, markets)
            self.assertEqual(network.call_count, 2)
            self.assertEqual(result["fetched"], 1)
            self.assertEqual(result["new_versions"], 1)
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("1: RuntimeError", result["errors"][0])
            self.assertIn("TimeoutError", result["errors"][0])
            saved = conn.execute("SELECT title FROM evidence").fetchone()[0]
            self.assertIn("market 2", saved)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
