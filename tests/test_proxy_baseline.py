from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polycast.evaluation import report
from polycast.paper import settle
from polycast.store import database, save_analysis_draft, save_prediction, upsert_markets


class FrozenProxyBaselineTests(unittest.TestCase):
    def test_report_pairs_only_proxy_seen_before_forecast(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            as_of = now - timedelta(minutes=10)
            reviewed_at = as_of + timedelta(seconds=20)
            market = {
                "id": "101", "event_id": "event-101", "event_title": "BTC five minutes",
                "event_slug": "btc-test", "question": "Bitcoin Up or Down?",
                "description": "Chainlink 60-second TWAP", "end_at": (now - timedelta(minutes=5)).isoformat(),
                "yes_token": "11", "no_token": "12", "liquidity": 10000,
                "volume_24h": 5000, "min_order_size": 5, "tick_size": .01,
                "fee_rate": .04, "category": "crypto5m", "yes_label": "Up",
                "no_label": "Down", "source_ref": f"BTCUSD:{int((now - timedelta(minutes=10)).timestamp())}",
            }
            evidence = {"source_id": "coinbase_btc_5m",
                        "summary": "spot-diffusion proxy P(Up) 0.3000. Coinbase is a proxy.",
                        "published_at": (as_of - timedelta(seconds=5)).isoformat(),
                        "first_seen_at": (as_of - timedelta(seconds=2)).isoformat()}
            payload = {"market_id": market["id"], "category": "crypto5m",
                       "as_of": as_of.isoformat(), "evidence": [evidence]}
            output = {"as_of": as_of.isoformat(), "p_yes": .4, "abstain": False,
                      "reasoning": "test", "reviewed_at": reviewed_at.isoformat(),
                      "review": {"approved": True}}
            with database(Path(folder) / "test.sqlite3") as conn:
                upsert_markets(conn, [market])
                save_analysis_draft(conn, "101", "rules", "evidence", payload, output)
                save_prediction(conn, "101", "rules", "evidence", output)
                settle(conn, "101", "YES", "https://gamma-api.polymarket.com/test",
                       (now - timedelta(minutes=4)).isoformat())
                scored = report(conn)
                self.assertEqual(scored["complete_proxy_matched_events"], 1)
                self.assertAlmostEqual(scored["risk_group_weighted_proxy_matched_model_brier"], .36)
                self.assertAlmostEqual(scored["risk_group_weighted_proxy_brier"], .49)
                self.assertEqual(scored["markets"][0]["proxy_p_yes"], .3)

                payload["evidence"][0]["first_seen_at"] = (as_of + timedelta(seconds=1)).isoformat()
                conn.execute("UPDATE analysis_drafts SET payload_json=?", (json.dumps(payload),))
                leaked = report(conn)
                self.assertEqual(leaked["complete_proxy_matched_events"], 0)
                self.assertIsNone(leaked["markets"][0]["proxy_p_yes"])


if __name__ == "__main__":
    unittest.main()
