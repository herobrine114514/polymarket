from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polycast.anchor_audit import audit_btc5m_anchors
from polycast.market import CHAINLINK_BTC_TWAP
from polycast.store import database


class AnchorAuditTests(unittest.TestCase):
    def test_includes_gamma_relay_disagreement_without_local_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start = datetime(2026, 9, 28, 3, 5, tzinfo=timezone.utc)
            now = start + timedelta(minutes=25)
            ticks = [(start, 85000), (start + timedelta(minutes=5), 85010),
                     (start + timedelta(minutes=10), 85005)]
            (root / "twap.jsonl").write_text("".join(json.dumps({
                "timestamp_ms": int(at.timestamp() * 1000),
                "full_accuracy_value": str(price * 10**18),
                "received_at_ms": int(at.timestamp() * 1000) + 7000}) + "\n"
                for at, price in ticks), encoding="utf-8")
            with database(root / "audit.sqlite3") as conn:
                remotes = {}
                for i in range(3):
                    window_start = start + timedelta(minutes=5 * i)
                    window_end = window_start + timedelta(minutes=5)
                    slug = f"btc-updown-5m-{int(window_start.timestamp())}"
                    ident = str(100 + i)
                    conn.execute("""INSERT INTO markets(id,event_id,event_title,event_slug,
                        question,description,end_at,yes_token,no_token,liquidity,volume_24h,
                        min_order_size,tick_size,fee_rate,category,yes_label,no_label,
                        source_ref,discovered_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ident, f"event-{ident}", "BTC", slug, "Up?", "TWAP rules",
                         window_end.isoformat(), f"yes-{ident}", f"no-{ident}",
                         10000, 1000, 5, .01, 0, "crypto5m", "Up", "Down",
                         f"BTCUSD:{int(window_start.timestamp())}",
                         start.isoformat(), start.isoformat()))
                    remotes[slug] = {"id": f"event-{ident}", "closed": True,
                        "markets": [{"id": ident, "closed": True,
                                     "description": "TWAP rules", "outcomes": '["Up","Down"]',
                                     "clobTokenIds": json.dumps([f"yes-{ident}", f"no-{ident}"]),
                                     "endDate": window_end.isoformat(),
                                     "resolutionSource": CHAINLINK_BTC_TWAP,
                                     "cryptoMarketConfig": {"id": "btc-5m-twap-60",
                                                            "twapLookbackSeconds": 60},
                                     "closedTime": (window_end + timedelta(minutes=1)).isoformat(),
                                     "outcomePrices": '["1","0"]' if i < 2 else '["0","1"]'}]}
                conn.execute("""INSERT INTO resolutions(market_id,winning_side,source_url,
                    resolved_at,recorded_at) VALUES(?,?,?,?,?)""",
                    ("100", "YES", "https://example.org/settled", now.isoformat(), now.isoformat()))
                report = audit_btc5m_anchors(conn, root / "twap.jsonl", now=now,
                    fetcher=lambda url: remotes[url.rsplit("/", 1)[-1]])
            self.assertEqual(report["selected_markets"], 3)
            self.assertEqual(report["possible_five_minute_windows_in_selected_span"], 3)
            self.assertEqual(report["totals"]["gamma_final"], 3)
            self.assertEqual(report["totals"]["relay_matched"], 1)
            self.assertEqual(report["totals"]["relay_disagreed"], 1)
            self.assertEqual(report["totals"]["relay_missing"], 1)
            mismatch = next(row for row in report["markets"] if row["market_id"] == "101")
            self.assertIsNone(mismatch["local_resolution"])
            self.assertEqual(mismatch["status"], "relay_disagreed")


if __name__ == "__main__":
    unittest.main()
