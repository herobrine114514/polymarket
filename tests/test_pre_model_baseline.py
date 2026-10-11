from __future__ import annotations

import tempfile
import unittest
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from polycast.cli import analyze, _pre_analysis_market_baseline
from polycast.config import Config
from polycast.evaluation import report
from polycast.store import SCHEMA, database, upsert_markets, save_analysis_draft


class PreModelBaselineTests(unittest.TestCase):
    def test_fast_draft_save_cannot_precede_its_subsecond_forecast(self) -> None:
        instant = datetime(2026, 10, 3, 9, 1, 26, 950000, tzinfo=timezone.utc)
        as_of = instant - timedelta(milliseconds=200)
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript(SCHEMA)
            upsert_markets(conn, [{'id': 'clock-test', 'event_id': 'clock-event',
                'event_title': 'Clock test', 'event_slug': 'clock-test',
                'question': 'BTC up?', 'description': 'test',
                'end_at': (instant + timedelta(minutes=3)).isoformat(),
                'yes_token': 'yes', 'no_token': 'no', 'liquidity': 10000,
                'volume_24h': 1000, 'min_order_size': 5, 'tick_size': .01,
                'fee_rate': 0, 'category': 'crypto5m'}])
            with patch('polycast.store.datetime', wraps=datetime) as clock:
                clock.now.return_value = instant
                save_analysis_draft(conn, 'clock-test', 'rules', 'evidence',
                                    {'as_of': as_of.isoformat()}, {'p_yes': .5})
            created_at = datetime.fromisoformat(conn.execute(
                'SELECT created_at FROM analysis_drafts').fetchone()[0])
        self.assertLessEqual(as_of, created_at)
        self.assertLess((created_at - as_of).total_seconds(), .3)

    def test_fractional_observation_and_quote_are_not_after_the_declared_capture(self) -> None:
        instant = datetime(2026, 10, 3, 8, 31, 33, 650000, tzinfo=timezone.utc)
        quote = instant - timedelta(milliseconds=100)
        observation = instant - timedelta(milliseconds=200)
        with patch('polycast.cli.datetime', wraps=datetime) as clock, patch(
                'polycast.cli.order_book', return_value={}), patch(
                'polycast.cli.book_baseline', return_value=(quote.isoformat(), .4, .6)):
            clock.now.return_value = instant
            captured = _pre_analysis_market_baseline(Config(), 'test-token')
        as_of = datetime.fromisoformat(captured['captured_at'])
        self.assertLessEqual(quote, as_of)
        self.assertLessEqual(observation, as_of)
        self.assertLess((as_of - quote).total_seconds(), .2)

    def test_book_precedes_model_and_failed_review_reuses_original_book(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(data_dir=Path(directory))
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=2)
            end = start + timedelta(minutes=5)
            market = {"id": "123", "event_id": "event-123", "event_title": "BTC 5m",
                      "event_slug": f"btc-updown-5m-{int(start.timestamp())}",
                      "question": "BTC up?", "description": "Chainlink TWAP rules",
                      "end_at": end.isoformat(), "yes_token": "yes-token", "no_token": "no-token",
                      "liquidity": 10000, "volume_24h": 1000, "min_order_size": 5,
                      "tick_size": .01, "fee_rate": 0, "category": "crypto5m",
                      "yes_label": "Up", "no_label": "Down",
                      "source_ref": f"BTCUSD:{int(start.timestamp())}"}
            evidence = [{"id": 1, "source": "Chainlink TWAP via Polymarket RTDS",
                         "source_id": "chainlink_btc_twap60", "title": "live TWAP",
                         "summary": "Opening and current observation", "published_at": now.isoformat(),
                         "first_seen_at": now.isoformat(), "url": "https://data.chain.link/streams/btc-usd"}]
            with database(config.database) as conn:
                upsert_markets(conn, [market])
            call_order = []

            def book(_token):
                call_order.append("book")
                first = call_order.count("book") == 1
                return {"timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                        "bids": [{"price": ".40" if first else ".10", "size": "10"}],
                        "asks": [{"price": ".60" if first else ".30", "size": "10"}]}

            def forecast(payload):
                call_order.append("analysis")
                return {"market_id": payload["market_id"], "as_of": payload["as_of"],
                        "p_yes": .7, "abstain": False, "reasoning": "test",
                        "assumptions": [], "supporting_evidence_ids": [1],
                        "counter_evidence_ids": [], "review_trigger": "new price"}

            def review(payload, _prediction):
                call_order.append("review")
                if call_order.count("review") == 1:
                    raise RuntimeError("temporary review failure")
                return {"market_id": payload["market_id"], "as_of": payload["as_of"],
                        "approved": True, "reason": "checked", "issues": [],
                        "checked_evidence_ids": [1]}

            with patch("polycast.cli.evidence_for_market", return_value=evidence), \
                    patch("polycast.cli.order_book", side_effect=book), \
                    patch("polycast.cli.run_codex", side_effect=forecast), \
                    patch("polycast.cli.run_review", side_effect=review):
                with self.assertRaisesRegex(RuntimeError, "temporary review failure"):
                    analyze(config, "123")
                result = analyze(config, "123")
            self.assertEqual(call_order, ["book", "analysis", "review", "book", "review"])
            self.assertEqual(result["market_baseline"]["phase"], "pre_analysis")
            self.assertEqual(result["market_baseline"]["p_yes_mid"], .5)
            with database(config.database) as conn:
                conn.execute("""INSERT INTO resolutions(market_id,winning_side,source_url,
                    resolved_at,recorded_at) VALUES(?,?,?,?,?)""",
                    ("123", "YES", "https://gamma-api.polymarket.com/events/slug/" + market["event_slug"],
                     (end + timedelta(minutes=1)).isoformat(), now.isoformat()))
                scored = report(conn)
                saved = conn.execute("""SELECT b.captured_at,p.as_of
                    FROM market_baselines b JOIN predictions p ON p.id=b.prediction_id
                    WHERE p.market_id='123'""").fetchone()
            self.assertEqual(saved["captured_at"], saved["as_of"])
            self.assertEqual(scored["complete_pre_model_matched_events"], 1)
            self.assertEqual(scored["risk_group_weighted_pre_model_matched_model_brier"], .09)
            self.assertEqual(scored["risk_group_weighted_pre_model_market_brier"], .25)


if __name__ == "__main__":
    unittest.main()
