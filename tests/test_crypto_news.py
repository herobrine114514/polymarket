from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polycast.analysis import hashes, make_payload
from polycast.config import Config
from polycast.paper import _blackout_reason, _prediction_current
from polycast.store import (database, evidence_for_market, get_market, insert_evidence,
                            save_prediction, sync_news_sources, upsert_markets,
                            withdraw_evidence, _upsert_source)


class CryptoNewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.temp.name))
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.market = {"id": "1", "event_id": "btc-event", "event_title": "Bitcoin 5m",
                       "event_slug": "btc-updown-5m-1", "question": "Bitcoin up?",
                       "description": "TWAP rules", "end_at": (self.now + timedelta(minutes=4)).isoformat(),
                       "yes_token": "up", "no_token": "down", "liquidity": 1000,
                       "volume_24h": 1000, "min_order_size": 5, "tick_size": .01,
                       "fee_rate": 0, "category": "crypto5m", "yes_label": "Up",
                       "no_label": "Down", "source_ref": "BTCUSD:1"}
        self.news = {"source_id": "news_wire", "source": "News Crypto: Wire",
                     "feed_url": "https://wire.test/rss", "license_scope": "summary_storage_and_model_analysis",
                     "license_reference": "https://wire.test/terms", "license_checked_at": self.now.isoformat()}
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            _upsert_source(conn, "chainlink_btc_twap60", "Chainlink TWAP", "official",
                           "https://data.chain.link/streams/btc-usd", "official_source", "", "", 1)
            self.add(conn, "price", source="Chainlink TWAP", source_id="chainlink_btc_twap60",
                     title="Opening TWAP", origin_story_id="BTCUSD:1", updated_at=self.now.isoformat(),
                     license_scope="official_source")
            sync_news_sources(conn, [self.news])

    def tearDown(self):
        self.temp.cleanup()

    def add(self, conn, name, *, published=None, first_seen=None, **fields):
        at = self.now - timedelta(minutes=1)
        item = {"source": "SEC Press", "url": f"https://source.test/{name}",
                "title": "Bitcoin digital asset announcement", "summary": "Official risk context",
                "published_at": (published or at).isoformat(), **fields}
        if item["source"] == self.news["source"]:
            item.update({k: self.news[k] for k in ("source_id", "feed_url", "license_scope")})
        insert_evidence(conn, [item])
        seen = (first_seen or at + timedelta(seconds=5)).isoformat()
        conn.execute("UPDATE evidence SET first_seen_at=? WHERE url=?", (seen, item["url"]))
        conn.execute("UPDATE evidence_current SET activated_at=? WHERE url=?", (seen, item["url"]))
        return item["url"]

    def predict(self, conn, key):
        market = get_market(conn, "1")
        evidence = [dict(item) for item in evidence_for_market(conn, market)]
        payload = make_payload(dict(market), evidence, self.now.isoformat())
        rules, inputs = hashes(payload, self.config.analysis_cooldown_hours)
        prediction_id = save_prediction(conn, "1", rules, inputs,
            {"as_of": self.now.isoformat(), "p_yes": .7, "abstain": False,
             "reviewed_at": self.now.isoformat(), "review": {"approved": True}})
        return conn.execute("SELECT * FROM predictions WHERE id=?", (prediction_id,)).fetchone()

    def test_five_minute_price_works_without_news_and_new_news_cools(self):
        with database(self.config.database) as conn:
            market = get_market(conn, "1")
            self.assertEqual(len(evidence_for_market(conn, market)), 1)
            prior = self.predict(conn, "before")
            self.assertTrue(_prediction_current(conn, market, prior, self.config))
            self.add(conn, "breaking")
            self.assertEqual(len(evidence_for_market(conn, market)), 2)
            self.assertIn("cooling window", _blackout_reason(conn, market, self.config))
            self.assertFalse(_prediction_current(conn, market, prior, self.config))
            current = self.predict(conn, "after")
            self.assertTrue(_prediction_current(conn, market, current, self.config))
            withdraw_evidence(conn, "https://source.test/breaking", "https://source.test/correction",
                              "Official announcement was withdrawn")
            self.assertFalse(_prediction_current(conn, market, current, self.config))
            self.assertEqual(len(evidence_for_market(conn, market)), 1)

    def test_licensed_crypto_news_removal_invalidates_frozen_prediction(self):
        with database(self.config.database) as conn:
            self.add(conn, "licensed", source=self.news["source"])
            market = get_market(conn, "1")
            current = self.predict(conn, "licensed")
            self.assertTrue(_prediction_current(conn, market, current, self.config))
            sync_news_sources(conn, [])
            self.assertEqual(len(evidence_for_market(conn, market)), 1)
            self.assertFalse(_prediction_current(conn, market, current, self.config))

    def test_cutoff_relevance_dedup_and_five_minute_lookback(self):
        with database(self.config.database) as conn:
            self.add(conn, "future-publication", published=self.now + timedelta(seconds=1))
            self.add(conn, "future-seen", first_seen=self.now + timedelta(seconds=1))
            self.add(conn, "irrelevant", title="Quarterly legal filing")
            self.add(conn, "old", published=self.now - timedelta(days=2),
                     first_seen=self.now - timedelta(days=2))
            self.add(conn, "original", origin_story_id="origin-1")
            self.add(conn, "reprint", source=self.news["source"], origin_story_id="origin-1")
            self.add(conn, "bad-scope", source=self.news["source"])
            conn.execute("UPDATE evidence SET license_scope='links_only' WHERE url LIKE '%bad-scope'")
            self.add(conn, "bad-feed", source=self.news["source"])
            conn.execute("UPDATE evidence SET feed_url='https://other.test/rss' WHERE url LIKE '%bad-feed'")
            market = get_market(conn, "1")
            short = evidence_for_market(conn, market, as_of=self.now.isoformat())
            self.assertEqual(len(short), 2)
            self.assertEqual(len([x for x in short if x["origin_story_id"] == "origin-1"]), 1)
            hourly = evidence_for_market(conn, {**dict(market), "category": "crypto"}, as_of=self.now.isoformat())
            self.assertIn("https://source.test/old", [x["url"] for x in hourly])


if __name__ == "__main__":
    unittest.main()
