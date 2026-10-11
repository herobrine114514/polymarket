from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from polycast.social import FED_DID, FED_FEED_URL, parse_fed_feed
from polycast.store import database, evidence_for_market, get_market, insert_evidence, upsert_markets


class OfficialSocialTests(unittest.TestCase):
    def test_only_verified_public_posts_use_appview_index_time(self) -> None:
        origin = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260928a.htm"
        post = {"uri": f"at://{FED_DID}/app.bsky.feed.post/abc123",
                "author": {"did": FED_DID, "handle": "federalreserve.gov", "labels": []},
                "indexedAt": "2026-09-28T03:21:00Z", "labels": [],
                "record": {"$type": "app.bsky.feed.post", "createdAt": "2026-09-28T03:20:00Z",
                           "text": "FOMC monetary policy statement",
                           "facets": [{"features": [{"uri": origin}]}]}}
        impostor = {**post, "author": {"did": "did:plc:impostor", "handle": "federalreserve.gov"}}
        hidden = {**post, "labels": [{"val": "!no-unauthenticated"}]}
        future = {**post, "indexedAt": "2026-09-28T04:00:00Z"}
        now = datetime(2026, 9, 28, 3, 30, tzinfo=timezone.utc)
        items = parse_fed_feed({"feed": [{"post": impostor}, {"post": hidden},
                                         {"post": future}, {"post": post}]}, now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["origin_story_id"], origin)
        self.assertEqual(items[0]["published_at"], "2026-09-28T03:21:00+00:00")
        self.assertEqual(items[0]["feed_published_at"], "2026-09-28T03:20:00+00:00")

    def test_social_repost_does_not_replace_official_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with database(Path(temp) / "test.sqlite3") as conn:
                upsert_markets(conn, [{"id": "1", "event_id": "fed-1",
                    "event_title": "Fed Decision in October?", "event_slug": "fed-1",
                    "question": "Will the Fed decrease interest rates?", "description": "rules",
                    "end_at": "2027-01-01T00:00:00Z", "yes_token": "100", "no_token": "200",
                    "liquidity": 10000., "volume_24h": 1000., "min_order_size": 5.,
                    "tick_size": .01, "fee_rate": .04}])
                original = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260928a.htm"
                insert_evidence(conn, [
                    {"source": "Federal Reserve", "url": original,
                     "title": "FOMC statement", "summary": "Policy unchanged",
                     "published_at": "2026-09-28T03:20:00+00:00"},
                    {"source": "Federal Reserve Bluesky",
                     "url": "https://bsky.app/profile/federalreserve.gov/post/abc123",
                     "title": "FOMC monetary policy statement", "summary": "See the official statement",
                     "origin_story_id": original, "feed_url": FED_FEED_URL,
                     "published_at": "2026-09-28T03:21:00+00:00"},
                    {"source": "Federal Reserve Bluesky",
                     "url": "https://bsky.app/profile/federalreserve.gov/post/def456",
                     "title": "FOMC statement on target range", "summary": "Firsthand policy comment",
                     "origin_story_id": f"at://{FED_DID}/app.bsky.feed.post/def456",
                     "feed_url": FED_FEED_URL,
                     "published_at": "2026-09-28T03:22:00+00:00"},
                    {"source": "Federal Reserve Bluesky",
                     "url": "https://bsky.app/profile/federalreserve.gov/post/edu789",
                     "title": "College challenge students study monetary policy",
                     "summary": "Education announcement",
                     "origin_story_id": "https://www.federalreserve.gov/aboutthefed/education.htm",
                     "feed_url": FED_FEED_URL,
                     "published_at": "2026-09-28T03:23:00+00:00"}])
                items = evidence_for_market(conn, get_market(conn, "1"))
                urls = {item["url"] for item in items}
                self.assertIn(original, urls)
                self.assertNotIn("https://bsky.app/profile/federalreserve.gov/post/abc123", urls)
                self.assertIn("https://bsky.app/profile/federalreserve.gov/post/def456", urls)
                self.assertNotIn("https://bsky.app/profile/federalreserve.gov/post/edu789", urls)


if __name__ == "__main__":
    unittest.main()
