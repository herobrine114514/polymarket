from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polycast.analysis import run_review
from polycast.short_cycle import _crypto_snapshot, reconcile_short
from polycast.store import database, upsert_markets


class ShortCycleTests(unittest.TestCase):
    def test_hourly_review_distinguishes_kline_open_close_and_minute_end(self) -> None:
        payload = {"market_id": "101", "as_of": "2026-09-28T08:45:00+00:00",
                   "category": "crypto", "evidence": [{"id": 1}]}
        prediction = {"market_id": "101", "as_of": payload["as_of"]}
        response = {"market_id": "101", "as_of": payload["as_of"],
                    "approved": True, "reason": "checked", "issues": [],
                    "checked_evidence_ids": [1]}
        with patch("polycast.analysis._run_json", return_value=response) as invoke:
            run_review(payload, prediction)
        self.assertIn("endTime 是选中 1 分钟 K 线的开盘时间", invoke.call_args.args[0])
        self.assertIn("不要把这三个不同时间戳误判为数据矛盾", invoke.call_args.args[0])

    def test_hourly_snapshot_uses_complete_minutes_and_exact_open(self) -> None:
        start = datetime(2026, 9, 27, 9, tzinfo=timezone.utc)
        now = start + timedelta(minutes=30, seconds=20)
        candles = []
        for minute in range(-120, 30):
            opening = 100 + minute * .01
            closing = opening + (.02 if minute % 2 else -.01)
            opened = int((start + timedelta(minutes=minute)).timestamp() * 1000)
            candles.append([opened, str(opening), "0", "0", str(closing), "0", opened + 59999])
        market = {"id": "101", "source_ref": f"BTCUSDT:{int(start.timestamp())}",
                  "end_at": (start + timedelta(hours=1)).isoformat()}
        with patch("polycast.short_cycle.fetch_json", return_value=candles) as fetch:
            evidence = _crypto_snapshot(market, now)
        self.assertIsNotNone(evidence)
        self.assertTrue(fetch.call_args.args[0].startswith(
            "https://data-api.binance.vision/api/v3/klines?"))
        self.assertIn("opens 2026-09-27T09:00:00+00:00 at 100.00000000", evidence["summary"])
        self.assertIn("open timestamp 2026-09-27T09:29:00+00:00 (the request endTime)", evidence["summary"])
        self.assertIn("close timestamp 2026-09-27T09:29:59.999+00:00", evidence["summary"])
        self.assertEqual(evidence["published_at"], (start + timedelta(minutes=30)).isoformat())
        self.assertEqual(evidence["origin_story_id"], market["source_ref"])
        candles[-1][6] += 60000
        with patch("polycast.short_cycle.fetch_json", return_value=candles):
            self.assertIsNone(_crypto_snapshot(market, now))

    def test_reconciliation_requires_gamma_and_binance_to_agree(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        market = {"id": "101", "event_id": "event-101", "event_title": "BTC hourly",
                  "event_slug": "btc-hourly-test", "question": "BTC hourly",
                  "description": "Exact hourly candle", "end_at": end.isoformat(),
                  "yes_token": "11", "no_token": "12", "liquidity": 9000,
                  "volume_24h": 1000, "min_order_size": 5, "tick_size": .01,
                  "fee_rate": .07, "category": "crypto", "yes_label": "Up",
                  "no_label": "Down", "source_ref": f"BTCUSDT:{int(start.timestamp())}"}
        closed = (end + timedelta(minutes=15)).isoformat()
        event = {"id": market["event_id"], "closed": True, "markets": [{
            "id": market["id"], "closed": True, "description": market["description"],
            "outcomes": '["Up","Down"]', "clobTokenIds": '["11","12"]',
            "closedTime": closed, "outcomePrices": '["1","0"]',
            "endDate": end.isoformat(),
            "resolutionSource": "https://www.binance.com/en/trade/BTC_USDT"}]}
        candle = [[int(start.timestamp()*1000), "100", "101", "99", "102", "10",
                   int(end.timestamp()*1000)-1]]
        def fake(url: str):
            return event if "gamma-api" in url else candle
        with database(Path(temp.name) / "test.db") as conn:
            upsert_markets(conn, [market])
            with patch("polycast.short_cycle.fetch_json", side_effect=fake):
                result = reconcile_short(conn)
            self.assertEqual(result["settled"][0]["winning_side"], "YES")
            self.assertEqual(conn.execute("SELECT winning_side FROM resolutions").fetchone()[0], "YES")
        # A conflicting final price must remain unresolved.
        with database(Path(temp.name) / "other.db") as conn:
            upsert_markets(conn, [market])
            event["markets"][0]["outcomePrices"] = '["0","1"]'
            with patch("polycast.short_cycle.fetch_json", side_effect=fake):
                result = reconcile_short(conn)
            self.assertFalse(result["settled"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
