from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from polycast.config import Config
from polycast.execution import fill_totals, match_levels
from polycast.paper import _quote, prepare_paper_intent
from polycast.store import database, now_iso, save_prediction, upsert_markets


class ExecutionFeeTests(unittest.TestCase):
    def test_multilevel_fee_uses_filled_prices_and_five_decimal_rounding(self):
        book = {"timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "asks": [{"price": ".8", "size": "9"}, {"price": ".2", "size": "2"}],
                "bids": [{"price": ".65", "size": "9"}]}
        with patch("polycast.paper.order_book", return_value=book):
            quote = _quote({"yes_token": "up", "fee_rate": .07}, "YES", 5, Config())
        self.assertAlmostEqual(quote["avg_ask"], .56)
        self.assertEqual(quote["fills"], [{"price": .2, "shares": 2}, {"price": .8, "shares": 3}])
        self.assertAlmostEqual(quote["fee_per_share"] * 5, .056)
        self.assertEqual(fill_totals(match_levels(book, 5, "bids"), 5, .07)[2], .07963)
        self.assertIsNone(match_levels(book, 20, "asks"))
        with self.assertRaisesRegex(ValueError, "shares mismatch"):
            fill_totals(quote["fills"], 6, .07)

    @patch("polycast.paper._prediction_current", return_value=True)
    @patch("polycast.paper.active_official_window", return_value=None)
    @patch("polycast.paper.assert_entry_allowed")
    def test_quote_fill_tampering_cannot_retain_average_or_reduce_fees(self, *_mocks):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(data_dir=Path(directory))
            market = {"id": "1", "event_id": "e1", "event_title": "Fed decision",
                      "event_slug": "test", "question": "Up?", "description": "Rules",
                      "end_at": "2027-01-01T00:00:00Z", "yes_token": "up", "no_token": "down",
                      "liquidity": 10000, "volume_24h": 1000, "min_order_size": 5,
                      "tick_size": .01, "fee_rate": .07}
            book = {"timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                    "asks": [{"price": ".2", "size": "2"}, {"price": ".8", "size": "3"}]}
            with database(config.database) as conn:
                upsert_markets(conn, [market])
                pid = save_prediction(conn, "1", "rules", "evidence",
                    {"as_of": now_iso(), "p_yes": .9, "abstain": False,
                     "review": {"approved": True}, "reviewed_at": now_iso(),
                     "valid_until": "2027-01-01T00:00:00Z"})
                with patch("polycast.paper.order_book", return_value=book):
                    quote = _quote(market, "YES", 5, config)
                quote.update({"market_id": "1", "event_id": "e1", "risk_group": "fed-policy",
                              "prediction_id": pid, "captured_at": now_iso(),
                              "total_cost": 2.856, "expected_edge": .2788})
                forged = {**quote, "fills": [{"price": .56, "shares": 5}]}
                with self.assertRaisesRegex(ValueError, "fill prices mismatch"):
                    prepare_paper_intent(conn, forged, config)
                forged = {**quote, "fee_per_share": 0, "total_cost": 2.8, "expected_edge": .29}
                with self.assertRaisesRegex(ValueError, "fees, cost, or edge mismatch"):
                    prepare_paper_intent(conn, forged, config)
                self.assertGreater(prepare_paper_intent(conn, quote, config), 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
