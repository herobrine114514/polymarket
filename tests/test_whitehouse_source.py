from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polycast.evidence import collect_official
from polycast.store import database, evidence_for_market, insert_evidence, upsert_markets


class WhiteHouseSourceTests(unittest.TestCase):
    def test_feed_cannot_promote_external_article_to_official_source(self) -> None:
        feed = "https://www.whitehouse.gov/presidential-actions/feed/"
        xml = b"""<rss><channel>
          <item><title>Digital assets</title><link>https://www.whitehouse.gov/presidential-actions/2026/09/crypto/</link><description>Official</description><pubDate>Mon, 28 Sep 2026 00:00:00 GMT</pubDate></item>
          <item><title>Impersonator</title><link>https://example.org/crypto/</link><description>External</description><pubDate>Mon, 28 Sep 2026 00:00:00 GMT</pubDate></item>
        </channel></rss>"""
        with patch("polycast.evidence.OFFICIAL_FEEDS", (("White House Presidential Actions", feed),)), patch(
                "polycast.evidence.fetch_bytes", return_value=xml):
            items, errors = collect_official()
        self.assertFalse(errors)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Digital assets")

    def test_crypto_research_uses_only_relevant_presidential_action(self) -> None:
        now = datetime.now(timezone.utc)
        market = {
            "id": "101", "event_id": "event-101", "event_title": "BTC hourly",
            "event_slug": "btc-hourly", "question": "Bitcoin Up or Down?",
            "description": "Binance BTCUSDT hourly close versus open",
            "end_at": (now + timedelta(minutes=20)).isoformat(),
            "yes_token": "11", "no_token": "12", "liquidity": 10000,
            "volume_24h": 5000, "min_order_size": 5, "tick_size": .01,
            "fee_rate": .04, "category": "crypto", "yes_label": "Up",
            "no_label": "Down", "source_ref": f"BTCUSDT:{int(now.timestamp())}",
        }
        feed = "https://www.whitehouse.gov/presidential-actions/feed/"
        def item(title: str, slug: str) -> dict:
            return {"source": "White House Presidential Actions", "feed_url": feed,
                    "url": f"https://www.whitehouse.gov/presidential-actions/2026/09/{slug}/",
                    "origin_story_id": f"https://www.whitehouse.gov/presidential-actions/2026/09/{slug}/",
                    "title": title, "summary": title,
                    "published_at": (now - timedelta(minutes=30)).isoformat()}
        with tempfile.TemporaryDirectory() as folder:
            with database(Path(folder) / "test.sqlite3") as conn:
                upsert_markets(conn, [market])
                insert_evidence(conn, [item("Executive Order on Digital Assets", "digital-assets"),
                                       item("National Fisheries Day", "fisheries")])
                selected = evidence_for_market(conn, conn.execute(
                    "SELECT * FROM markets WHERE id='101'").fetchone())
                self.assertEqual([row["title"] for row in selected],
                                 ["Executive Order on Digital Assets"])


if __name__ == "__main__":
    unittest.main()
