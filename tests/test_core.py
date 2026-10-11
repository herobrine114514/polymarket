from __future__ import annotations

import tempfile
import unittest
import json
import io
import math
import os
import subprocess
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from polycast.analysis import (OUTPUT_SCHEMA, ProviderUnavailable, _run_deepseek, _run_json, hashes,
                               make_event_payload, make_payload, run_event_codex)
from polycast.blackout import active_window, evidence_cooling
from polycast.calendar import (active_official_window, parse_bls_calendar,
                               parse_fed_calendar, refresh_calendars)
from polycast.cli import _capture_market_baseline, analyze, cycle, main
from polycast.config import Config
from polycast.evidence import collect_configured, collect_official, parse_bls_release, parse_rss
from polycast.evaluation import report
from polycast.market import (book_baseline, discover, executable_ask, executable_bid,
                             complete_fed_event, fed_outcome_slot, supported_policy_market)
from polycast.paper import (_quote, candidate, expire_paper_intents,
                            place, prepare_paper_intent, settle)
from polycast.risk import (active_halts, assert_entry_allowed, mark_account,
                           resume_after_reconciliation)
from polycast.runner import local_cycle_lock, run_recorded
from polycast.store import (balance, database, evidence_for_market, get_market, insert_evidence, now_iso,
                            finish_model_call, model_calls_today, reserve_model_call,
                            save_market_baseline, save_prediction, sync_news_sources,
                            upsert_markets, withdraw_evidence)


def unchanged_execution_fixture(market, quote, config):
    from polycast.store import digest_text
    return quote, {"frozen_quote_sha256":digest_text(quote),"execution_quote_sha256":digest_text(quote)}


class OrderBookTests(unittest.TestCase):
    def test_extreme_ask_is_skipped_before_order_validation(self) -> None:
        market = {"yes_token": "yes", "no_token": "no", "fee_rate": .04}
        book = {"timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "asks": [{"price": "0.5", "size": "5"}]}
        with patch("polycast.paper.order_book", return_value=book), \
                patch("polycast.paper.fill_totals", return_value=(2.5, 0.5000000000000001, .05)):
            self.assertIsNone(_quote(market, "YES", 5, Config()))

    def test_buy_uses_lowest_asks_even_when_api_orders_descending(self) -> None:
        book = {"asks": [{"price": "0.70", "size": "10"},
                         {"price": "0.51", "size": "2"},
                         {"price": "0.52", "size": "8"}]}
        self.assertEqual(executable_ask(book, 5), ((2 * .51 + 3 * .52) / 5, .52))
        self.assertIsNone(executable_ask(book, 25))

    def test_liquidation_uses_highest_bids_and_requires_depth(self) -> None:
        book = {"bids": [{"price": ".30", "size": "10"},
                         {"price": ".50", "size": "2"},
                         {"price": ".40", "size": "8"}]}
        self.assertEqual(executable_bid(book, 5), ((2 * .5 + 3 * .4) / 5, .4))
        self.assertIsNone(executable_bid(book, 25))

    def test_market_baseline_uses_fresh_best_bid_and_ask(self) -> None:
        book = {"timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "bids": [{"price": ".40", "size": "5"}, {"price": ".45", "size": "2"}],
                "asks": [{"price": ".60", "size": "3"}, {"price": ".55", "size": "1"}]}
        snapshot = book_baseline(book, 60)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot[1:], (.45, .55))
        book["timestamp"] = "0"
        self.assertIsNone(book_baseline(book, 60))


class MarketScopeTests(unittest.TestCase):
    def test_meeting_decisions_are_selected_without_numeric_count_markets(self) -> None:
        self.assertTrue(supported_policy_market(
            "Fed Decision in October?",
            "Will there be no change in Fed interest rates after the October 2026 meeting?"))
        self.assertFalse(supported_policy_market(
            "How many Fed rate cuts in 2026?", "Will 11 Fed rate cuts happen in 2026?"))
        end_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        def market(market_id: str, question: str) -> dict:
            return {"id": market_id, "question": question, "clobTokenIds": '["11","12"]',
                    "outcomes": '["Yes","No"]', "closed": False, "active": True,
                    "acceptingOrders": True, "enableOrderBook": True,
                    "endDate": end_at, "volume24hr": "100",
                    "liquidity": "10000"}
        decisions = [
            market(str(index), question) for index, question in enumerate((
                "Will the Fed decrease interest rates by 25 bps after the October 2026 meeting?",
                "Will the Fed decrease interest rates by 50+ bps after the October 2026 meeting?",
                "Will the Fed increase interest rates by 25 bps after the October 2026 meeting?",
                "Will the Fed increase interest rates by 50+ bps after the October 2026 meeting?",
                "Will there be no change in Fed interest rates after the October 2026 meeting?"), 1)]
        events = [
            {"id": "e1", "title": "Fed Decision in October?", "endDate": end_at,
             "liquidity": "10000", "volume24hr": "100", "markets": decisions},
            {"id": "e2", "title": "How many Fed rate cuts in 2026?",
             "endDate": end_at, "liquidity": "10000",
             "volume24hr": "100000", "markets": [
                 market("99", "Will 11 Fed rate cuts happen in 2026?")]},
        ]
        with patch("polycast.market.fetch_json", return_value={"events": events}):
            selected = discover()
        self.assertEqual({row["id"] for row in selected}, {"1", "2", "3", "4", "5"})


class JointFedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.temp.name))
        questions = (
            "Will the Fed decrease interest rates by 50+ bps after the October 2026 meeting?",
            "Will the Fed decrease interest rates by 25 bps after the October 2026 meeting?",
            "Will there be no change in Fed interest rates after the October 2026 meeting?",
            "Will the Fed increase interest rates by 25 bps after the October 2026 meeting?",
            "Will the Fed increase interest rates by 50+ bps after the October 2026 meeting?",
        )
        self.markets = [{"id": str(index), "event_id": "fed-event",
                         "event_title": "Fed Decision in October?", "event_slug": "fed-october",
                         "question": question, "description": "common settlement rules",
                         "end_at": "2027-01-01T00:00:00Z", "yes_token": str(100 + index),
                         "no_token": str(200 + index), "liquidity": 10000.,
                         "volume_24h": 1000., "min_order_size": 5., "tick_size": .01,
                         "fee_rate": .04} for index, question in enumerate(questions, 1)]
        with database(self.config.database) as conn:
            upsert_markets(conn, self.markets)
            insert_evidence(conn, [{"source": "Federal Reserve",
                                    "url": "https://www.federalreserve.gov/test-joint",
                                    "title": "FOMC statement", "summary": "Rates unchanged",
                                    "published_at": "2026-09-01T00:00:00+00:00"}])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_official_fed_speech_is_available_to_policy_forecast(self) -> None:
        url = "https://www.federalreserve.gov/newsevents/speech/test2026.htm"
        testimony_url = "https://www.federalreserve.gov/newsevents/testimony/test2026.htm"
        with database(self.config.database) as conn:
            insert_evidence(conn, [{"source": "Federal Reserve Speeches", "url": url,
                                    "title": "Economic outlook and policy", "summary": "Speech text",
                                    "published_at": now_iso(),
                                    "feed_url": "https://www.federalreserve.gov/feeds/speeches.xml"}])
            insert_evidence(conn, [{"source": "Federal Reserve Speeches",
                                    "url": "https://www.federalreserve.gov/newsevents/speech/irrelevant.htm",
                                    "title": "Bank regulatory stress testing", "summary": "Unrelated topic",
                                    "published_at": now_iso(),
                                    "feed_url": "https://www.federalreserve.gov/feeds/speeches.xml"}])
            insert_evidence(conn, [{"source": "Federal Reserve Testimony", "url": testimony_url,
                                    "title": "Semiannual Monetary Policy Report to Congress",
                                    "summary": "Policy outlook", "published_at": now_iso(),
                                    "feed_url": "https://www.federalreserve.gov/feeds/testimony.xml"},
                                   {"source": "Federal Reserve Testimony",
                                    "url": "https://www.federalreserve.gov/newsevents/testimony/irrelevant.htm",
                                    "title": "Bank supervisory examination", "summary": "Unrelated topic",
                                    "published_at": now_iso(),
                                    "feed_url": "https://www.federalreserve.gov/feeds/testimony.xml"}])
            evidence = evidence_for_market(conn, get_market(conn, "1"))
            self.assertIn(url, [item["url"] for item in evidence])
            self.assertIn(testimony_url, [item["url"] for item in evidence])
            self.assertNotIn("https://www.federalreserve.gov/newsevents/speech/irrelevant.htm",
                             [item["url"] for item in evidence])
            self.assertNotIn("https://www.federalreserve.gov/newsevents/testimony/irrelevant.htm",
                             [item["url"] for item in evidence])
            source = conn.execute("SELECT kind,enabled FROM source_registry WHERE source_id='fed_speeches'").fetchone()
            self.assertEqual((source["kind"], source["enabled"]), ("official", 1))
            testimony_source = conn.execute(
                "SELECT kind,enabled FROM source_registry WHERE source_id='fed_testimony'").fetchone()
            self.assertEqual((testimony_source["kind"], testimony_source["enabled"]), ("official", 1))

    def test_outcome_set_is_complete_and_strict(self) -> None:
        self.assertTrue(complete_fed_event(self.markets))
        self.assertEqual({fed_outcome_slot(row["question"]) for row in self.markets},
                         {"decrease_50_plus", "decrease_25", "no_change",
                          "increase_25", "increase_50_plus"})
        self.assertFalse(complete_fed_event(self.markets[:-1]))
        self.assertFalse(supported_policy_market("Fed Decision in October?",
                         "Will the Fed increase interest rates by 75 bps after the October meeting?"))

    def test_joint_model_normalizes_rounding_but_rejects_incoherence(self) -> None:
        with database(self.config.database) as conn:
            evidence = [dict(row) for row in evidence_for_market(conn, get_market(conn, "1"))]
        payload = make_event_payload(self.markets, evidence, now_iso())
        market_ids = [row["market_id"] for row in payload["markets"]]
        forecast = {"event_id": payload["event_id"], "as_of": payload["as_of"],
                    "probabilities": {market_id: .202 for market_id in market_ids},
                    "abstain": False, "reasoning": "test", "assumptions": [],
                    "supporting_evidence_ids": [evidence[0]["id"]],
                    "counter_evidence_ids": [], "review_trigger": "new release"}
        with patch("polycast.analysis._run_json", return_value=forecast):
            normalized = run_event_codex(payload)
        self.assertAlmostEqual(sum(normalized["probabilities"].values()), 1.)
        self.assertEqual(normalized["raw_probabilities"][market_ids[0]], .202)
        forecast["probabilities"] = {market_id: .3 for market_id in market_ids}
        with patch("polycast.analysis._run_json", return_value=forecast):
            with self.assertRaisesRegex(ValueError, "do not sum to one"):
                run_event_codex(payload)

    def test_cycle_produces_one_reviewed_joint_distribution_and_blocks_tampering(self) -> None:
        def forecast(payload: dict) -> dict:
            return {"event_id": payload["event_id"], "as_of": payload["as_of"],
                    "probabilities": {row["market_id"]: .2 for row in payload["markets"]},
                    "raw_probabilities": {row["market_id"]: .2 for row in payload["markets"]},
                    "abstain": False, "reasoning": "joint estimate", "assumptions": [],
                    "supporting_evidence_ids": [], "counter_evidence_ids": [],
                    "review_trigger": "new statement"}
        def review(payload: dict, _forecast: dict) -> dict:
            return {"event_id": payload["event_id"], "as_of": payload["as_of"],
                    "approved": True, "reason": "checked", "issues": [],
                    "checked_evidence_ids": []}
        baseline_at = now_iso()
        with patch("polycast.cli.scan", return_value=self.markets), patch(
                "polycast.cli.collect", return_value={"fetched": 0, "new_versions": 0,
                                                       "errors": []}), patch(
                "polycast.cli.refresh_calendars", return_value={}), patch(
                "polycast.cli.run_event_codex", side_effect=forecast) as model, patch(
                "polycast.cli.run_event_review", side_effect=review) as reviewer, patch(
                "polycast.cli._pre_analysis_market_baseline", return_value={
                    "status": "captured", "captured_at": baseline_at,
                    "book_timestamp": baseline_at, "bid": .3, "ask": .5}), patch(
                "polycast.cli.paper", return_value={"status": "skipped"}) as paper_call:
            result = cycle(self.config)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(paper_call.call_count, 5)
        self.assertEqual(result["analyses"][0]["status"], "analyzed")
        with database(self.config.database) as conn:
            self.assertEqual(model_calls_today(conn), 2)
            rows = conn.execute("SELECT id,market_id,probability,model_output_json FROM predictions").fetchall()
            self.assertEqual(len(rows), 5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM market_baselines").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM analysis_market_baselines").fetchone()[0], 5)
            self.assertAlmostEqual(sum(row["probability"] for row in rows), 1.)
            self.assertEqual(len({json.loads(row["model_output_json"])["joint_forecast_id"]
                                  for row in rows}), 1)
            with patch("polycast.paper._blackout_reason", return_value=None), patch(
                    "polycast.paper.live_market", return_value={}) as live:
                quote, reason = candidate(conn, "1", self.config)
            self.assertIsNone(quote)
            self.assertIn("Market rules", reason)
            live.assert_called_once()
            conn.execute("UPDATE predictions SET probability=.3 WHERE market_id='2'")
            with patch("polycast.paper._blackout_reason", return_value=None), patch(
                    "polycast.paper.live_market") as live:
                quote, reason = candidate(conn, "1", self.config)
            self.assertIsNone(quote)
            self.assertIn("probabilities are inconsistent", reason)
            live.assert_not_called()
            conn.execute("UPDATE predictions SET probability=.1 WHERE market_id='3'")
            with patch("polycast.paper._blackout_reason", return_value=None), patch(
                    "polycast.paper.live_market") as live:
                quote, reason = candidate(conn, "1", self.config)
            self.assertIsNone(quote)
            self.assertIn("probabilities are inconsistent", reason)
            live.assert_not_called()

    def test_event_report_waits_for_all_outcomes_and_uses_first_public_result_cutoff(self) -> None:
        now = datetime.now(timezone.utc)
        forecast_at = (now - timedelta(hours=3)).isoformat(timespec="seconds")
        reviewed_at = (now - timedelta(hours=2)).isoformat(timespec="seconds")
        baseline_at = (now - timedelta(minutes=90)).isoformat(timespec="seconds")
        first_outcome = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        later_outcome = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
        probabilities = {row["id"]: (.6 if row["id"] == "3" else .1)
                         for row in self.markets}
        with database(self.config.database) as conn:
            prior_ids = {}
            for market in self.markets:
                output = {"as_of": forecast_at, "p_yes": probabilities[market["id"]],
                          "abstain": False, "reasoning": "joint estimate",
                          "joint_event_id": "fed-event", "joint_forecast_id": "joint-1",
                          "joint_probabilities": probabilities,
                          "model_version": "deepseek-flash", "provider": "deepseek",
                          "review": {"approved": True}, "reviewed_at": reviewed_at}
                prior_ids[market["id"]] = save_prediction(
                    conn, market["id"], "rules", "evidence-1", output)
                save_market_baseline(conn, prior_ids[market["id"]], baseline_at,
                                     baseline_at, .19, .21)
            source = "https://www.federalreserve.gov/newsevents/pressreleases/test.htm"
            settle(conn, "1", "NO", source, first_outcome)
            partial = report(conn)
            self.assertEqual(partial["scored_markets"], 1)
            self.assertEqual(partial["complete_scored_events"], 0)
            self.assertIsNone(partial["risk_group_weighted_brier"])
            late = {"as_of": (now - timedelta(minutes=40)).isoformat(timespec="seconds"),
                    "p_yes": .9, "abstain": False, "reasoning": "after first outcome",
                    "joint_event_id": "fed-event", "joint_forecast_id": "joint-late",
                    "joint_probabilities": probabilities,
                    "model_version": "deepseek-v4-pro", "provider": "deepseek",
                    "review": {"approved": True},
                    "reviewed_at": (now - timedelta(minutes=35)).isoformat(timespec="seconds")}
            save_prediction(conn, "2", "rules", "evidence-late", late)
            for market in self.markets[1:]:
                settle(conn, market["id"], "YES" if market["id"] == "3" else "NO",
                       source, later_outcome)
            complete = report(conn)
        self.assertEqual(complete["complete_scored_events"], 1)
        self.assertEqual(complete["by_model"]["deepseek-flash"]["complete_scored_events"], 1)
        self.assertEqual(complete["by_model"]["deepseek-v4-pro"]["complete_scored_events"], 0)
        self.assertEqual(complete["complete_matched_events"], 1)
        self.assertEqual(complete["scored_markets"], 5)
        self.assertEqual(next(row for row in complete["markets"] if row["market_id"] == "2")
                         ["prediction_id"], prior_ids["2"])
        group = complete["groups"][0]
        self.assertTrue(group["complete_for_validation"])
        self.assertAlmostEqual(group["joint_brier"], .2)
        self.assertAlmostEqual(group["joint_market_brier"], .8)
        self.assertAlmostEqual(group["joint_log_loss"], -math.log(.6), places=5)


class ProviderFailureTests(unittest.TestCase):
    def test_deepseek_schema_failure_reports_paths_without_model_content(self) -> None:
        malformed = {"market_id": "101", "as_of": "2026-09-28T00:00:00+00:00",
                     "p_yes": .5, "abstain": False, "reasoning": "test",
                     "assumptions": "wrong type", "supporting_evidence_ids": [],
                     "counter_evidence_ids": [], "secret_payload": "SENSITIVE_MODEL_CONTENT"}
        raw = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(malformed)}}]}
        with patch("polycast.analysis.deepseek_key", return_value="test-key"), patch(
                "polycast.analysis.urlopen"), patch("polycast.analysis.json.load", return_value=raw):
            with self.assertRaises(RuntimeError) as caught:
                _run_deepseek("SENSITIVE_PROMPT", OUTPUT_SCHEMA, 10, Path("."))
        message = str(caught.exception)
        self.assertIn("$.review_trigger: missing", message)
        self.assertIn("$.assumptions: expected array", message)
        self.assertIn("unexpected field(s)", message)
        self.assertNotIn("SENSITIVE_MODEL_CONTENT", message)
        self.assertNotIn("SENSITIVE_PROMPT", message)
        self.assertNotIn("secret_payload", message)

    def test_usage_limit_error_is_classified_without_prompt_leak(self) -> None:
        message = "ERROR: You've hit your usage limit. Try again at Sep 30th, 2026 6:59 PM"
        with patch("polycast.analysis.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = message
            with self.assertRaises(ProviderUnavailable) as caught:
                _run_json("SENSITIVE_PROMPT", {})
        if os.name == "nt":
            self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertNotIn("SENSITIVE_PROMPT", str(caught.exception))
        self.assertGreater(caught.exception.retry_at, datetime.now(timezone.utc))


class EvidenceParsingTests(unittest.TestCase):
    def test_atom_entry_keeps_publication_time_and_https_source(self) -> None:
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <title>Employment Situation</title><link href="https://www.bls.gov/news.release/empsit.nr0.htm"/>
          <published>2026-09-01T12:00:00Z</published><summary>Payroll report</summary>
          </entry><entry><title>Unsafe</title><link href="http://example.com/unsafe"/>
          <published>2026-09-01T12:00:00Z</published></entry></feed>'''
        rows = parse_rss(feed, "BLS Employment", "https://www.bls.gov/feed/empsit.rss")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["published_at"], "2026-09-01T12:00:00+00:00")
        self.assertEqual(rows[0]["summary"], "Payroll report")

    def test_bls_embargo_header_sets_public_release_time(self) -> None:
        article = b'''<html><div id="bodytext"><h1>Consumer Price Index</h1>
          Transmission of material in this release is embargoed until
          8:30 a.m. (ET) Friday, September 11, 2026
          <p>All items rose 0.4 percent; core rose 0.3 percent.</p>
          </div></html>'''
        text, public_at = parse_bls_release(article)
        self.assertEqual(public_at, datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc))
        self.assertIn("core rose 0.3 percent", text)

    def test_recent_bls_rss_is_rejected_before_verified_embargo_lift(self) -> None:
        feed_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        public_at = datetime.now(timezone.utc) + timedelta(hours=2)
        local = public_at.astimezone(ZoneInfo("America/New_York"))
        am_pm = "a.m." if local.hour < 12 else "p.m."
        header = (f"{local.hour % 12 or 12}:{local.minute:02d} {am_pm} (ET) "
                  f"{local.strftime('%A, %B')} {local.day}, {local.year}")
        feed = (f"<rss><channel><item><title>Employment Situation</title>"
                f"<link>https://www.bls.gov/news.release/archives/empsit_test.htm</link>"
                f"<description>Payroll rose</description>"
                f"<pubDate>{format_datetime(feed_time, usegmt=True)}</pubDate>"
                f"</item></channel></rss>").encode()
        article = (f'<div id="bodytext">Transmission of material in this release '
                   f'is embargoed until {header} Payroll rose 200,000.</div>').encode()
        def fake_fetch(url: str, **_kwargs) -> bytes:
            return feed if url.endswith(".rss") else article
        with patch("polycast.evidence.OFFICIAL_FEEDS", (("BLS Employment", "https://www.bls.gov/feed/empsit.rss"),)), patch(
                "polycast.evidence.fetch_bytes", side_effect=fake_fetch):
            items, errors = collect_official()
        self.assertEqual(items, [])
        self.assertTrue(any("embargoed BLS release" in error for error in errors))

    def test_recent_bls_rss_without_verifiable_release_time_is_rejected(self) -> None:
        feed_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        feed = (f"<rss><channel><item><title>Consumer Price Index</title>"
                f"<link>https://www.bls.gov/news.release/archives/cpi_test.htm</link>"
                f"<description>CPI rose</description>"
                f"<pubDate>{format_datetime(feed_time, usegmt=True)}</pubDate>"
                f"</item></channel></rss>").encode()
        with patch("polycast.evidence.OFFICIAL_FEEDS", (("BLS CPI", "https://www.bls.gov/feed/cpi.rss"),)), patch(
                "polycast.evidence.fetch_bytes", side_effect=[feed, b'<div id="bodytext">CPI rose</div>']):
            items, errors = collect_official()
        self.assertEqual(items, [])
        self.assertTrue(any("without verified public time" in error for error in errors))

    def test_fed_testimony_only_keeps_relevant_official_article(self) -> None:
        url = "https://www.federalreserve.gov/newsevents/testimony/policy-test.htm"
        published = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1), usegmt=True)
        feed = ("<rss><channel>"
                f"<item><title>Semiannual Monetary Policy Report</title><link>{url}</link>"
                f"<pubDate>{published}</pubDate></item>"
                f"<item><title>Monetary Policy</title><link>https://example.com/pretend-fed.htm</link>"
                f"<pubDate>{published}</pubDate></item>"
                f"<item><title>Bank supervision</title>"
                f"<link>https://www.federalreserve.gov/newsevents/testimony/supervision.htm</link>"
                f"<pubDate>{published}</pubDate></item>"
                "</channel></rss>").encode()

        def fake_fetch(target: str, **_kwargs) -> bytes:
            if target.endswith("/feeds/testimony.xml"):
                return feed
            if target == url:
                return (b'<div id="article">Monetary policy testimony full text. '
                        b'Inflation and employment conditions inform the rate outlook.</div>')
            raise AssertionError(f"Unexpected article request: {target}")

        with patch("polycast.evidence.OFFICIAL_FEEDS", (("Federal Reserve Testimony",
                    "https://www.federalreserve.gov/feeds/testimony.xml"),)), patch(
                    "polycast.evidence.fetch_bytes", side_effect=fake_fetch):
            items, errors = collect_official()
        self.assertEqual(errors, [])
        self.assertEqual([item["url"] for item in items], [url])
        self.assertIn("full text", items[0]["summary"])

    def test_release_blackout_and_fresh_evidence_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "blackouts.json"
            path.write_text('''[{"name":"CPI release","starts_at":"2026-09-26T12:00:00Z",
                "ends_at":"2026-09-26T12:30:00Z","risk_groups":["fed-policy"]}]''', encoding="utf-8")
            instant = datetime(2026, 9, 26, 12, 10, tzinfo=timezone.utc)
            self.assertEqual(active_window(path, "fed-policy", instant), "CPI release")
            self.assertIsNone(active_window(path, "other", instant))
            self.assertTrue(evidence_cooling("2026-09-26T12:05:00Z", "2026-09-26T12:00:00Z", 15, instant))
            self.assertFalse(evidence_cooling("2026-09-26T12:05:00Z", "2026-08-01T12:00:00Z", 15, instant))
            path.write_text('[{"name":"bad","starts_at":"2026-09-26T12:00:00",'
                            '"ends_at":"2026-09-26T12:30:00Z","risk_groups":["all"]}]', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "timezone"):
                active_window(path, "fed-policy", instant)


class EvidenceProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.market = {
            "id": "feed-test", "event_id": "event-1", "event_title": "Fed decision",
            "event_slug": "fed-decision", "question": "Will the Fed hold rates?",
            "description": "rules", "end_at": "2027-01-01T00:00:00Z",
            "yes_token": "11", "no_token": "12", "liquidity": 10000.,
            "volume_24h": 1000., "min_order_size": 5., "tick_size": .01,
            "fee_rate": .04,
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _licensed_config(self) -> dict:
        return {"source_id": "policy_wire", "name": "Policy Wire",
                "url": "https://news.example.test/feed.xml", "topic": "fed",
                "license_confirmed": True,
                "license_scope": "summary_storage_and_model_analysis",
                "license_reference": "agreement-2026-09-01",
                "license_checked_at": "2026-09-27T00:00:00Z"}

    def test_configured_news_requires_explicit_analysis_storage_scope(self) -> None:
        feed = b'''<rss><channel><item><title>Fed report</title>
          <link>https://news.example.test/fed-report</link>
          <guid>https://origin.example.test/story-1</guid>
          <description>Summary</description>
          <pubDate>Sat, 26 Sep 2026 12:00:00 GMT</pubDate>
          </item></channel></rss>'''
        config_path = self.path / "sources.json"
        config = self._licensed_config()
        config_path.write_text(json.dumps([config]), encoding="utf-8")
        with patch("polycast.evidence.fetch_bytes", return_value=feed):
            items, errors, sources = collect_configured(config_path)
        self.assertEqual(errors, [])
        self.assertEqual(items[0]["origin_story_id"], "https://origin.example.test/story-1")
        self.assertEqual(sources[0]["source_id"], "news_policy_wire")
        with database(self.path / "db.sqlite3") as conn:
            upsert_markets(conn, [self.market])
            sync_news_sources(conn, sources)
            self.assertEqual(insert_evidence(conn, items), 1)
            self.assertEqual(insert_evidence(conn, items), 0)
            self.assertEqual(len(evidence_for_market(conn, get_market(conn, "feed-test"))), 1)
            row = conn.execute("SELECT * FROM evidence WHERE url=?", (items[0]["url"],)).fetchone()
            self.assertEqual(row["license_scope"], "summary_storage_and_model_analysis")
            self.assertIsNotNone(row["fetched_at"])
            self.assertIsNotNone(row["last_seen_at"])
            sync_news_sources(conn, [])
            self.assertEqual(evidence_for_market(conn, get_market(conn, "feed-test")), [])
        config["license_scope"] = "links_only"
        config_path.write_text(json.dumps([config]), encoding="utf-8")
        with patch("polycast.evidence.fetch_bytes") as fetch:
            items, errors, sources = collect_configured(config_path)
        self.assertEqual(items, [])
        self.assertEqual(sources, [])
        self.assertTrue(errors)
        fetch.assert_not_called()

    def test_withdrawal_preserves_archive_and_blocks_old_forecast(self) -> None:
        article = "https://www.federalreserve.gov/pressreleases/test.htm"
        source_url = "https://www.federalreserve.gov/pressreleases/correction.htm"
        with database(self.path / "db.sqlite3") as conn:
            upsert_markets(conn, [self.market])
            insert_evidence(conn, [{"source": "Federal Reserve", "url": article,
                                    "title": "Statement", "summary": "Old report",
                                    "published_at": "2026-09-26T12:00:00+00:00"}])
            market = get_market(conn, "feed-test")
            evidence = evidence_for_market(conn, market)
            self.assertEqual(len(evidence), 1)
            rules_hash, evidence_hash = hashes(make_payload(dict(market), [dict(evidence[0])], now_iso()))
            save_prediction(conn, "feed-test", rules_hash, evidence_hash, {
                "p_yes": .8, "abstain": False, "reasoning": "test",
                "review": {"approved": True}, "reviewed_at": now_iso(),
                "valid_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
            self.assertTrue(withdraw_evidence(conn, article, source_url, "Official correction withdrew statement"))
            self.assertFalse(withdraw_evidence(conn, article, source_url, "Official correction withdrew statement"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence WHERE url=?", (article,)).fetchone()[0], 1)
            self.assertEqual(evidence_for_market(conn, market), [])
            with patch("polycast.paper._blackout_reason", return_value=None):
                quote, reason = candidate(conn, "feed-test", Config(data_dir=self.path))
            self.assertIsNone(quote)
            self.assertIn("Prediction evidence or review window changed", reason)

    def test_corrected_article_reversion_requires_new_review(self) -> None:
        article = "https://www.federalreserve.gov/pressreleases/revision.htm"
        base = {"source": "Federal Reserve", "url": article,
                "title": "Statement", "published_at": "2026-09-26T12:00:00+00:00"}
        with database(self.path / "db.sqlite3") as conn:
            upsert_markets(conn, [self.market])
            self.assertEqual(insert_evidence(conn, [{**base, "summary": "First text"}]), 1)
            market = get_market(conn, "feed-test")
            first = evidence_for_market(conn, market)[0]
            first_hash = hashes(make_payload(dict(market), [dict(first)], now_iso()))[1]
            self.assertEqual(insert_evidence(conn, [{**base, "summary": "Corrected text"}]), 1)
            corrected = evidence_for_market(conn, market)[0]
            self.assertNotEqual(first["id"], corrected["id"])
            self.assertEqual(insert_evidence(conn, [{**base, "summary": "First text"}]), 0)
            restored = evidence_for_market(conn, market)[0]
            self.assertEqual(restored["id"], first["id"])
            self.assertEqual(restored["activation_seq"], 3)
            restored_hash = hashes(make_payload(dict(market), [dict(restored)], now_iso()))[1]
            self.assertNotEqual(first_hash, restored_hash)


class OfficialCalendarTests(unittest.TestCase):
    @staticmethod
    def bls_calendar(cpi_day: int = 14) -> bytes:
        return (f"""BEGIN:VCALENDAR
BEGIN:VEVENT
UID:cpi-2026-10
DTSTART;TZID=US-Eastern:202610{cpi_day:02d}T083000
SUMMARY:Consumer Price Index
END:VEVENT
BEGIN:VEVENT
UID:jobs-2026-11
DTSTART;TZID=US-Eastern:20261106T083000
SUMMARY:Employment Situation
END:VEVENT
BEGIN:VEVENT
UID:cpi-2026-11
DTSTART;TZID=US-Eastern:20261110T083000
SUMMARY:Consumer Price Index
END:VEVENT
END:VCALENDAR
""").encode()

    @staticmethod
    def fed_calendar() -> bytes:
        return b"""<a>2026 FOMC Meetings</a>
<div class="fomc-meeting__month"><strong>October</strong></div>
<div class="fomc-meeting__date">27-28</div>
<a>2025 FOMC Meetings</a>"""

    def test_official_parsers_preserve_eastern_dst_and_full_meeting_days(self) -> None:
        now = datetime(2026, 10, 14, 11, tzinfo=timezone.utc)
        bls = parse_bls_calendar(self.bls_calendar(), now)
        self.assertEqual(bls[0]["event_at"], "2026-10-14T12:30:00+00:00")
        self.assertEqual(bls[1]["event_at"], "2026-11-06T13:30:00+00:00")
        fed = parse_fed_calendar(self.fed_calendar(), now)
        self.assertEqual(fed[0]["starts_at"], "2026-10-27T04:00:00+00:00")
        self.assertEqual(fed[0]["ends_at"], "2026-10-29T04:00:00+00:00")

    def test_refresh_revisions_and_stale_schedule_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Config(data_dir=Path(temp))
            now = datetime(2026, 10, 14, 11, tzinfo=timezone.utc)
            def fetch(url: str, **_kwargs) -> bytes:
                return self.bls_calendar() if "bls.gov" in url else self.fed_calendar()
            with database(config.database) as conn, patch(
                    "polycast.calendar.fetch_bytes", side_effect=fetch):
                result = refresh_calendars(conn, now=now)
                self.assertEqual(result["BLS"]["windows"], 3)
                self.assertIn("Consumer Price Index", active_official_window(
                    conn, "fed-policy", now + timedelta(hours=1, minutes=15)))
                with self.assertRaisesRegex(ValueError, "stale"):
                    active_official_window(conn, "fed-policy", now + timedelta(days=2))
                with patch("polycast.calendar.fetch_bytes", side_effect=lambda url, **kwargs:
                           self.bls_calendar(15) if "bls.gov" in url else self.fed_calendar()):
                    refresh_calendars(conn, force=True, now=now + timedelta(hours=7))
                revisions = conn.execute("""SELECT COUNT(*) FROM calendar_revisions
                    WHERE source='BLS' AND event_uid='cpi-2026-10'""").fetchone()[0]
                self.assertEqual(revisions, 2)
                event_time = conn.execute("""SELECT event_at FROM calendar_windows
                    WHERE source='BLS' AND event_uid='cpi-2026-10'""").fetchone()[0]
                self.assertEqual(event_time, "2026-10-15T12:30:00+00:00")

    def test_fomc_meeting_window_blocks_on_second_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Config(data_dir=Path(temp))
            now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
            with database(config.database) as conn, patch(
                    "polycast.calendar.fetch_bytes", side_effect=lambda url, **kwargs:
                    self.bls_calendar() if "bls.gov" in url else self.fed_calendar()):
                refresh_calendars(conn, now=now)
                self.assertIn("FOMC meeting", active_official_window(
                    conn, "fed-policy", now + timedelta(hours=6)))
                self.assertIsNone(active_official_window(
                    conn, "us-macro-data", now + timedelta(hours=6)))


class CycleRunnerTests(unittest.TestCase):
    def test_cycle_failure_is_audited_and_next_run_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Config(data_dir=Path(temp))
            def fail() -> dict:
                raise RuntimeError("network unavailable")
            with self.assertRaisesRegex(RuntimeError, "network unavailable"):
                run_recorded(config, fail)
            with database(config.database) as conn:
                failed = conn.execute(
                    "SELECT status,error_count FROM cycle_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                self.assertEqual((failed["status"], failed["error_count"]), ("failed", 1))
            outcome = run_recorded(config, lambda: {
                "watchlist_markets": 3,
                "sources": {"fetched": 2, "new_versions": 1, "errors": []},
                "calendars": {}, "analyses": [], "paper_checks": []})
            self.assertEqual(outcome["status"], "succeeded")
            with database(config.database) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM cycle_runs").fetchone()[0], 2)
                self.assertIsNone(conn.execute(
                    "SELECT name FROM cycle_leases WHERE name='cycle'").fetchone())

    def test_os_lock_prevents_overlapping_cycle_and_recovers_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Config(data_dir=Path(temp))
            with local_cycle_lock(config.data_dir / "cycle.lock"):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    run_recorded(config, lambda: {})
            with database(config.database) as conn:
                conn.execute("""INSERT INTO cycle_runs(owner_token,started_at,status,
                    config_hash,prompt_version,model_version,model_run_id_before)
                    VALUES(?,?,'running',?,?,?,0)""",
                    ("old-owner", now_iso(), "config", "prompt", "model"))
            run_recorded(config, lambda: {"watchlist_markets": 0, "sources": {},
                                          "calendars": {}, "analyses": [], "paper_checks": []})
            with database(config.database) as conn:
                self.assertEqual(conn.execute(
                    "SELECT status FROM cycle_runs WHERE owner_token='old-owner'"
                ).fetchone()[0], "abandoned")

    def test_watch_continues_after_one_failed_iteration(self) -> None:
        output = io.StringIO()
        with patch("polycast.cli.run_recorded", side_effect=[
                RuntimeError("temporary scan failure"),
                {"run_id": 2, "status": "succeeded", "summary": {"error_count": 0}}]), patch(
                "polycast.cli.time.sleep"), redirect_stdout(output):
            self.assertEqual(main(["watch", "--interval-minutes", "1", "--max-cycles", "2"]), 0)
        values = []
        remaining = output.getvalue()
        decoder = json.JSONDecoder()
        while remaining.strip():
            value, consumed = decoder.raw_decode(remaining.lstrip())
            values.append(value)
            remaining = remaining.lstrip()[consumed:]
        self.assertEqual([value["status"] for value in values], ["failed", "succeeded"])


class AtomicPaperRiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.temp.name), initial_cash=1000,
                             max_market_fraction=.005, max_event_fraction=.02,
                             max_total_fraction=.10)
        self.market = {
            "id": "101", "event_id": "e1", "event_title": "Fed decision",
            "event_slug": "fed-decision", "question": "No change?", "description": "rules",
            "end_at": "2027-01-01T00:00:00Z", "yes_token": "11", "no_token": "12",
            "liquidity": 10000., "volume_24h": 1000., "min_order_size": 5.,
            "tick_size": .01, "fee_rate": .04,
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def reviewed_prediction(probability: float) -> dict:
        return {"p_yes": probability, "abstain": False, "reasoning": "test",
                "assumptions": [], "supporting_evidence_ids": [], "counter_evidence_ids": [],
                "review": {"approved": True},
                "reviewed_at": now_iso(),
                "valid_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}

    def _insert_open_order(self, conn) -> None:
        upsert_markets(conn, [self.market])
        prediction_id = save_prediction(
            conn, "101", "rules", "evidence", self.reviewed_prediction(.8))
        conn.execute("""INSERT INTO paper_orders(prediction_id,market_id,event_id,risk_group,
            side,created_at,shares,avg_price,fee,total_cost,expected_edge)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (prediction_id, "101", "e1", "fed-policy", "YES", now_iso(),
             5., .6, .025, 3.025, .1))

    def test_closed_unsettled_position_marks_zero_without_order_book(self) -> None:
        self.market["end_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with database(self.config.database) as conn:
            mark_account(conn, self.config)
            self._insert_open_order(conn)
            with patch("polycast.risk.order_book", side_effect=RuntimeError("HTTP 404")) as book:
                snapshot = mark_account(conn, self.config)
            book.assert_not_called()
            self.assertEqual(snapshot["liquidation_value"], 0)
            self.assertEqual(snapshot["active_halts"], [])
            self.assertEqual(conn.execute("SELECT status FROM paper_orders").fetchone()[0], "open")

    def test_fresh_liquidation_mark_triggers_persistent_daily_loss_stop(self) -> None:
        config = replace(self.config, max_daily_loss_fraction=.002)
        with database(config.database) as conn:
            mark_account(conn, config)
            self._insert_open_order(conn)
            with self.assertRaisesRegex(ValueError, "positions changed"):
                assert_entry_allowed(conn, config)
            book = {"timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                    "bids": [{"price": ".10", "size": "5"}]}
            with patch("polycast.risk.order_book", return_value=book):
                marked = mark_account(conn, config)
            self.assertIn("daily_loss", marked["active_halts"])
            self.assertLess(marked["equity"], config.initial_cash * .998)
            with self.assertRaisesRegex(ValueError, "daily_loss"):
                assert_entry_allowed(conn, config)
            book["bids"] = [{"price": ".90", "size": "5"}]
            with patch("polycast.risk.order_book", return_value=book):
                mark_account(conn, config)
            self.assertEqual([row["reason"] for row in active_halts(conn)], ["daily_loss"])
            with patch("polycast.risk.order_book", return_value=book):
                recovered = resume_after_reconciliation(
                    conn, config, "Reconciled order and verified fresh executable bid")
            self.assertEqual(recovered["cleared_halts"], 1)
            self.assertEqual(active_halts(conn), [])

    def test_valuation_failure_halts_new_risk(self) -> None:
        with database(self.config.database) as conn:
            mark_account(conn, self.config)
            self._insert_open_order(conn)
            with patch("polycast.risk.order_book", side_effect=OSError("feed offline")):
                with self.assertRaisesRegex(RuntimeError, "valuation unavailable"):
                    mark_account(conn, self.config)
            with self.assertRaisesRegex(ValueError, "valuation_unavailable"):
                assert_entry_allowed(conn, self.config)
            book = {"timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                    "bids": [{"price": ".60", "size": "5"}]}
            with patch("polycast.risk.order_book", return_value=book):
                recovered = resume_after_reconciliation(
                    conn, self.config, "Feed restored and paper ledger reconciled")
            self.assertEqual(recovered["cleared_halts"], 1)
            assert_entry_allowed(conn, self.config)

    def test_drawdown_stop_uses_high_water_equity(self) -> None:
        config = replace(self.config, max_daily_loss_fraction=.5,
                         max_drawdown_fraction=.002)
        with database(config.database) as conn:
            mark_account(conn, config)
            self._insert_open_order(conn)
            book = {"timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                    "bids": [{"price": ".10", "size": "5"}]}
            with patch("polycast.risk.order_book", return_value=book):
                marked = mark_account(conn, config)
            self.assertIn("drawdown", marked["active_halts"])
            self.assertNotIn("daily_loss", marked["active_halts"])
            with patch("polycast.risk.order_book", return_value=book):
                with self.assertRaisesRegex(ValueError, "threshold is still breached"):
                    resume_after_reconciliation(
                        conn, config, "Reviewed the drawdown but price is still low")

    def test_scan_archives_departed_market_without_deleting_history(self) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            prediction_id = save_prediction(conn, "101", "rules", "evidence-1", self.reviewed_prediction(.75))
            replacement = {**self.market, "id": "102"}
            upsert_markets(conn, [replacement])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM markets WHERE in_watchlist=1").fetchone()[0], 1)
            with self.assertRaises(KeyError):
                get_market(conn, "101")
            self.assertEqual(conn.execute("SELECT id FROM predictions WHERE market_id='101'").fetchone()[0], prediction_id)

    @patch("polycast.paper.active_official_window", return_value=None)
    @patch("polycast.paper.assert_entry_allowed")
    @patch("polycast.paper._prediction_current", return_value=True)
    @patch("polycast.paper.refresh_execution", side_effect=unchanged_execution_fixture)
    def test_market_cap_and_idempotent_prediction(self, _execution, _current, _risk, _calendar) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            first = save_prediction(conn, "101", "rules", "evidence-1", self.reviewed_prediction(.75))
            second = save_prediction(conn, "101", "rules", "evidence-2", self.reviewed_prediction(.75))
            quote = {"market_id": "101", "event_id": "e1", "risk_group": "fed-policy",
                     "prediction_id": second, "side": "YES", "captured_at": now_iso(),
                     "book_timestamp": now_iso(), "shares": 5., "avg_ask": .6,
                     "worst_ask": .6, "fee_per_share": .0096,
                     "fee_method": "per_level_5dp_v1", "fills": [{"price": .6, "shares": 5.}],
                     "total_cost": 3.048, "expected_edge": .0904}
            self.assertGreater(place(conn, quote, self.config), 0)
            intent = conn.execute("""SELECT i.id,i.status,o.intent_id FROM trade_intents i
                JOIN paper_orders o ON o.intent_id=i.id WHERE i.prediction_id=?""",
                (second,)).fetchone()
            self.assertEqual(intent["status"], "filled")
            self.assertEqual(intent["id"], intent["intent_id"])
            self.assertEqual([row["to_status"] for row in conn.execute(
                "SELECT to_status FROM trade_intent_events WHERE intent_id=? ORDER BY id",
                (intent["id"],))], ["reserved", "filled"])
            with self.assertRaisesRegex(ValueError, "already used"):
                place(conn, quote, self.config)
            quote["prediction_id"] = first
            with self.assertRaisesRegex(ValueError, "superseded"):
                place(conn, quote, self.config)
            third = save_prediction(conn, "101", "rules", "evidence-3", self.reviewed_prediction(.9))
            quote["prediction_id"] = third
            quote["total_cost"] = 3.048  # Existing $3.048 + new cost exceeds $5 market cap.
            quote["expected_edge"] = .2404
            with self.assertRaisesRegex(ValueError, "Market risk limit"):
                place(conn, quote, self.config)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 1)
            save_prediction(conn, "101", "rules", "unreviewed", {
                "p_yes": .99, "abstain": False, "reasoning": "unreviewed",
                "assumptions": [], "supporting_evidence_ids": [], "counter_evidence_ids": []})
            self.assertEqual(candidate(conn, "101", self.config)[1],
                             "Prediction has no approved independent review")

    @patch("polycast.paper.active_official_window", return_value=None)
    @patch("polycast.paper.assert_entry_allowed")
    @patch("polycast.paper._prediction_current", return_value=True)
    def test_reserved_and_unknown_intents_hold_group_budget_across_restarts(
            self, _current, _risk, _calendar) -> None:
        config = replace(self.config, max_event_fraction=.005)
        with database(config.database) as conn:
            other = {**self.market, "id": "102", "event_id": "e2"}
            upsert_markets(conn, [self.market, other])
            first = save_prediction(conn, "101", "rules", "evidence-1", self.reviewed_prediction(.8))
            second = save_prediction(conn, "102", "rules", "evidence-2", self.reviewed_prediction(.8))
            def quote(market_id: str, event_id: str, prediction_id: int) -> dict:
                return {"market_id": market_id, "event_id": event_id,
                        "risk_group": "fed-policy", "prediction_id": prediction_id,
                        "side": "YES", "captured_at": now_iso(), "book_timestamp": now_iso(),
                        "shares": 5., "avg_ask": .6, "worst_ask": .6,
                        "fee_per_share": .0096, "total_cost": 3.048,
                        "fee_method": "per_level_5dp_v1", "fills": [{"price": .6, "shares": 5.}],
                        "expected_edge": .1404}
            first_quote = quote("101", "e1", first)
            second_quote = quote("102", "e2", second)
            intent_id = prepare_paper_intent(conn, first_quote, config)
            self.assertEqual(prepare_paper_intent(conn, first_quote, config), intent_id)
            self.assertAlmostEqual(balance(conn, config.initial_cash)["reserved_cost"], 3.048)
            with self.assertRaisesRegex(ValueError, "Event group risk limit"):
                prepare_paper_intent(conn, second_quote, config)
            conn.execute("UPDATE trade_intents SET expires_at=? WHERE id=?",
                         ("2000-01-01T00:00:00+00:00", intent_id))
            self.assertEqual(expire_paper_intents(conn), 1)
            self.assertEqual(balance(conn, config.initial_cash)["reserved_cost"], 0)
            second_intent = prepare_paper_intent(conn, second_quote, config)
            conn.execute("UPDATE trade_intents SET status='unknown' WHERE id=?", (second_intent,))
            self.assertEqual(expire_paper_intents(conn), 0)
            self.assertAlmostEqual(balance(conn, config.initial_cash)["reserved_cost"], 3.048)
            with self.assertRaisesRegex(ValueError, "reserved or unknown intent"):
                prepare_paper_intent(conn, second_quote, config)

    @patch("polycast.paper.active_official_window", return_value=None)
    @patch("polycast.paper.assert_entry_allowed")
    @patch("polycast.paper._prediction_current", return_value=True)
    def test_changed_market_after_reservation_releases_paper_budget(
            self, _current, _risk, _calendar) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            prediction_id = save_prediction(
                conn, "101", "rules", "evidence", self.reviewed_prediction(.8))
            quote = {"market_id": "101", "event_id": "e1", "risk_group": "fed-policy",
                     "prediction_id": prediction_id, "side": "YES", "captured_at": now_iso(),
                     "book_timestamp": now_iso(), "shares": 5., "avg_ask": .6,
                     "worst_ask": .6, "fee_per_share": .0096,
                     "fee_method": "per_level_5dp_v1", "fills": [{"price": .6, "shares": 5.}],
                     "total_cost": 3.048, "expected_edge": .1404}
            intent_id = prepare_paper_intent(conn, quote, self.config)
            conn.execute("UPDATE markets SET description='changed rules' WHERE id='101'")
            with patch("polycast.paper._prediction_current", return_value=False):
                with self.assertRaisesRegex(ValueError, "evidence or review window changed"):
                    place(conn, quote, self.config)
            self.assertEqual(conn.execute(
                "SELECT status FROM trade_intents WHERE id=?", (intent_id,)).fetchone()[0],
                "rejected")
            self.assertEqual(balance(conn, self.config.initial_cash)["reserved_cost"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 0)

    @patch("polycast.paper.active_official_window", return_value=None)
    @patch("polycast.paper.assert_entry_allowed")
    @patch("polycast.paper._prediction_current", return_value=True)
    def test_forged_quote_cannot_change_risk_group_or_fee(self, _current, _risk, _calendar) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            prediction_id = save_prediction(
                conn, "101", "rules", "evidence", self.reviewed_prediction(.8))
            quote = {"market_id": "101", "event_id": "e1", "risk_group": "other",
                     "prediction_id": prediction_id, "side": "YES", "captured_at": now_iso(),
                     "book_timestamp": now_iso(), "shares": 5., "avg_ask": .6,
                     "worst_ask": .6, "fee_per_share": .0096,
                     "fee_method": "per_level_5dp_v1", "fills": [{"price": .6, "shares": 5.}], "total_cost": 3.048,
                     "expected_edge": .1404}
            with self.assertRaisesRegex(ValueError, "risk group mismatch"):
                place(conn, quote, self.config)
            quote["risk_group"] = "fed-policy"
            quote["fee_per_share"] = 0.
            with self.assertRaisesRegex(ValueError, "fees, cost, or edge mismatch"):
                place(conn, quote, self.config)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 0)

    def test_daily_model_cap_counts_failed_calls(self) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            first = reserve_model_call(conn, "101", "analysis", "test-model", 2)
            finish_model_call(conn, first, "test failure")
            second = reserve_model_call(conn, "101", "review", "test-model", 2)
            finish_model_call(conn, second)
            self.assertEqual(model_calls_today(conn), 2)
            with self.assertRaisesRegex(RuntimeError, "Daily Codex call limit"):
                reserve_model_call(conn, "101", "analysis", "test-model", 2)

    def test_scheduled_blackout_blocks_paper_before_market_api(self) -> None:
        now = datetime.now(timezone.utc)
        (self.config.data_dir / "blackouts.json").write_text(json.dumps([{
            "name": "test release", "starts_at": (now - timedelta(minutes=1)).isoformat(),
            "ends_at": (now + timedelta(minutes=10)).isoformat(),
            "risk_groups": ["fed-policy"]}]), encoding="utf-8")
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            save_prediction(conn, "101", "rules", "evidence-1", self.reviewed_prediction(.8))
            self.assertEqual(candidate(conn, "101", self.config)[1],
                             "Scheduled release blackout: test release")

    def test_cycle_rechecks_price_when_evidence_unchanged(self) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            insert_evidence(conn, [{"source": "Federal Reserve", "url": "https://www.federalreserve.gov/test",
                                    "title": "FOMC statement", "summary": "Rate held",
                                    "published_at": "2026-09-01T00:00:00+00:00"}])
            row = conn.execute("SELECT * FROM markets WHERE id='101'").fetchone()
            evidence = evidence_for_market(conn, row)
            rule_hash, evidence_hash = hashes(make_payload(dict(row), [dict(e) for e in evidence], now_iso()))
            save_prediction(conn, "101", rule_hash, evidence_hash, self.reviewed_prediction(.75))
        with patch("polycast.cli.scan", return_value=[self.market]), patch(
            "polycast.cli.collect", return_value={"fetched": 0, "new_versions": 0, "errors": []}
        ), patch("polycast.cli.refresh_calendars", return_value={}), patch(
            "polycast.cli.paper", return_value={"status": "skipped"}) as check:
            result = cycle(self.config)
        check.assert_called_once_with(self.config, "101")
        self.assertEqual(result["analyses"], [])
        self.assertEqual(result["paper_checks"], [{"status": "skipped"}])

    def test_cycle_collects_without_model_calls_during_provider_pause(self) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            insert_evidence(conn, [{"source": "Federal Reserve",
                                    "url": "https://www.federalreserve.gov/test",
                                    "title": "FOMC statement", "summary": "Rate held",
                                    "published_at": "2026-09-01T00:00:00+00:00"}])
            pause = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            conn.execute("INSERT INTO meta(key,value) VALUES('model_pause_until',?)", (pause,))
        with patch("polycast.cli.scan", return_value=[self.market]), patch(
                "polycast.cli.collect", return_value={
                    "fetched": 0, "new_versions": 0, "errors": []}), patch(
                "polycast.cli.refresh_calendars", return_value={}), patch(
                "polycast.cli.run_codex") as model:
            result = cycle(self.config)
        self.assertEqual(result["analyses"], [])
        self.assertEqual(result["model_status"]["reason"], "provider_paused")
        model.assert_not_called()

    @patch("polycast.paper.active_official_window", return_value=None)
    @patch("polycast.paper.assert_entry_allowed")
    def test_new_evidence_blocks_old_prediction_before_quote_or_fill(self, _risk, _calendar) -> None:
        config = replace(self.config, evidence_cooldown_minutes=0)
        with database(config.database) as conn:
            upsert_markets(conn, [self.market])
            item = {"source": "Federal Reserve", "url": "https://www.federalreserve.gov/test",
                    "title": "FOMC statement", "summary": "Rate held",
                    "published_at": "2026-09-01T00:00:00+00:00"}
            insert_evidence(conn, [item])
            market = get_market(conn, "101")
            evidence = evidence_for_market(conn, market)
            rules_hash, evidence_hash = hashes(
                make_payload(dict(market), [dict(e) for e in evidence], now_iso()),
                config.analysis_cooldown_hours)
            prediction_id = save_prediction(
                conn, "101", rules_hash, evidence_hash, self.reviewed_prediction(.8))
            insert_evidence(conn, [{**item, "summary": "Rate cut",
                                    "published_at": "2026-09-02T00:00:00+00:00"}])
            with patch("polycast.paper.live_market") as live:
                self.assertEqual(candidate(conn, "101", config)[1],
                                 "Prediction evidence or review window changed")
                live.assert_not_called()
            quote = {"market_id": "101", "event_id": "e1", "risk_group": "fed-policy",
                     "prediction_id": prediction_id, "side": "YES", "captured_at": now_iso(),
                     "book_timestamp": now_iso(), "shares": 5., "avg_ask": .6,
                     "worst_ask": .6, "fee_per_share": .0096,
                     "fee_method": "per_level_5dp_v1", "fills": [{"price": .6, "shares": 5.}], "total_cost": 3.048,
                     "expected_edge": .1404}
            with self.assertRaisesRegex(ValueError, "evidence or review window changed"):
                place(conn, quote, config)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 0)

    def test_completed_prediction_captures_market_probability(self) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            prediction_id = save_prediction(
                conn, "101", "rules", "evidence", self.reviewed_prediction(.7))
        book = {"timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "bids": [{"price": ".42", "size": "5"}],
                "asks": [{"price": ".48", "size": "5"}]}
        with patch("polycast.cli.order_book", return_value=book) as fetch:
            result = _capture_market_baseline(self.config, "101", prediction_id)
        fetch.assert_called_once_with("11")
        self.assertEqual(result["p_yes_mid"], .45)
        with database(self.config.database) as conn:
            baseline = conn.execute(
                "SELECT p_yes_mid FROM market_baselines WHERE prediction_id=?", (prediction_id,)
            ).fetchone()
            self.assertAlmostEqual(baseline["p_yes_mid"], .45)

    def test_rejected_independent_review_forces_abstention(self) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            insert_evidence(conn, [{"source": "Federal Reserve", "url": "https://www.federalreserve.gov/test",
                                    "title": "FOMC statement", "summary": "Rate held",
                                    "published_at": "2026-09-01T00:00:00+00:00"}])

        def forecast(payload: dict) -> dict:
            return {"market_id": payload["market_id"], "as_of": payload["as_of"], "p_yes": .8,
                    "abstain": False, "reasoning": "unsupported", "assumptions": [],
                    "supporting_evidence_ids": [], "counter_evidence_ids": [], "review_trigger": "new statement"}

        def reject(payload: dict, prediction: dict) -> dict:
            return {"market_id": payload["market_id"], "as_of": payload["as_of"],
                    "approved": False, "reason": "unsupported claim", "issues": ["missing source"],
                    "checked_evidence_ids": []}

        with patch("polycast.cli.run_codex", side_effect=forecast), patch(
                "polycast.cli.run_review", side_effect=reject), patch(
                "polycast.cli._capture_market_baseline", return_value={"status": "unavailable"}):
            outcome = analyze(self.config, "101")
        self.assertTrue(outcome["abstain"])
        self.assertFalse(outcome["review_approved"])
        with database(self.config.database) as conn:
            prediction = conn.execute("SELECT probability,model_output_json FROM predictions").fetchone()
            self.assertIsNone(prediction["probability"])
            self.assertEqual(json.loads(prediction["model_output_json"])["p_yes"], .8)
            self.assertEqual(model_calls_today(conn), 2)

    def test_failed_review_reuses_saved_analysis_draft(self) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            insert_evidence(conn, [{"source": "Federal Reserve", "url": "https://www.federalreserve.gov/test",
                                    "title": "FOMC statement", "summary": "Rate held",
                                    "published_at": "2026-09-01T00:00:00+00:00"}])

        def forecast(payload: dict) -> dict:
            return {"market_id": payload["market_id"], "as_of": payload["as_of"], "p_yes": .8,
                    "abstain": False, "reasoning": "test", "assumptions": [],
                    "supporting_evidence_ids": [], "counter_evidence_ids": [], "review_trigger": "new data"}

        def approve(payload: dict, prediction: dict) -> dict:
            return {"market_id": payload["market_id"], "as_of": payload["as_of"],
                    "approved": True, "reason": "checked", "issues": [], "checked_evidence_ids": []}

        attempts = iter([False, True])

        def review(payload: dict, prediction: dict) -> dict:
            if not next(attempts):
                raise RuntimeError("temporary review failure")
            return approve(payload, prediction)

        with patch("polycast.cli.run_codex", side_effect=forecast) as analysis_call, patch(
                "polycast.cli.run_review", side_effect=review), patch(
                "polycast.cli._capture_market_baseline", return_value={"status": "unavailable"}):
            with self.assertRaisesRegex(RuntimeError, "temporary review failure"):
                analyze(self.config, "101")
            result = analyze(self.config, "101")
        analysis_call.assert_called_once()
        self.assertFalse(result["abstain"])
        with database(self.config.database) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM analysis_drafts").fetchone()[0], 1)
            self.assertEqual(model_calls_today(conn), 3)

    def test_provider_limit_pauses_retries_without_another_call(self) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            insert_evidence(conn, [{"source": "Federal Reserve", "url": "https://www.federalreserve.gov/test",
                                    "title": "FOMC statement", "summary": "Rate held",
                                    "published_at": "2026-09-01T00:00:00+00:00"}])
        retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
        with patch("polycast.cli.run_codex", side_effect=ProviderUnavailable(retry_at)) as call:
            with self.assertRaises(ProviderUnavailable):
                analyze(self.config, "101")
            with self.assertRaisesRegex(RuntimeError, "provider paused"):
                analyze(self.config, "101")
        call.assert_called_once()
        with database(self.config.database) as conn:
            self.assertEqual(model_calls_today(conn), 1)
            self.assertIsNotNone(conn.execute("SELECT value FROM meta WHERE key='model_pause_until'").fetchone())

    @patch("polycast.paper.active_official_window", return_value=None)
    @patch("polycast.paper.assert_entry_allowed")
    @patch("polycast.paper._prediction_current", return_value=True)
    @patch("polycast.paper.refresh_execution", side_effect=unchanged_execution_fixture)
    def test_verified_outcomes_score_separate_events_and_settle_paper_order(self, _execution, _current, _risk, _calendar) -> None:
        source = "https://www.federalreserve.gov/newsevents/pressreleases/test.htm"
        with database(self.config.database) as conn:
            other = {**self.market, "id": "102", "question": "Hike?", "event_id": "e2"}
            upsert_markets(conn, [self.market, other])
            codex_prediction = {**self.reviewed_prediction(.8), "provider": "codex"}
            deepseek_prediction = {**self.reviewed_prediction(.3), "provider": "deepseek"}
            first = save_prediction(conn, "101", "rules", "evidence-1", codex_prediction)
            second = save_prediction(conn, "102", "rules", "evidence-2", deepseek_prediction)
            save_market_baseline(conn, first, now_iso(), now_iso(), .5, .7)
            save_market_baseline(conn, second, now_iso(), now_iso(), .3, .5)
            quote = {"market_id": "101", "event_id": "e1", "risk_group": "fed-policy",
                     "prediction_id": first, "side": "YES", "captured_at": now_iso(),
                     "book_timestamp": now_iso(), "shares": 5., "avg_ask": .6,
                     "worst_ask": .6, "fee_per_share": .0096,
                     "fee_method": "per_level_5dp_v1", "fills": [{"price": .6, "shares": 5.}],
                     "total_cost": 3.048, "expected_edge": .1404}
            place(conn, quote, self.config)
            resolved_at = now_iso()
            self.assertEqual(settle(conn, "101", "YES", source, resolved_at), 1)
            self.assertEqual(settle(conn, "101", "YES", source, resolved_at), 0)
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                settle(conn, "101", "NO", source, resolved_at)
            self.assertEqual(settle(conn, "102", "NO", source, resolved_at), 0)
            self.assertEqual(candidate(conn, "101", self.config)[1], "Market outcome already recorded")
            result = report(conn)
        self.assertEqual(result["resolved_markets"], 2)
        self.assertEqual(result["scored_risk_groups"], 2)
        self.assertEqual([group["scored_markets"] for group in result["groups"]], [1, 1])
        self.assertAlmostEqual(result["risk_group_weighted_brier"], .065)
        self.assertEqual(result["market_baseline_scored_markets"], 2)
        self.assertAlmostEqual(result["risk_group_weighted_matched_model_brier"], .065)
        self.assertAlmostEqual(result["risk_group_weighted_market_brier"], .16)
        self.assertEqual(result["by_provider"]["codex"]["complete_scored_events"], 1)
        self.assertEqual(result["by_provider"]["deepseek"]["complete_scored_events"], 1)
        self.assertAlmostEqual(result["by_provider"]["codex"]["risk_group_weighted_brier"], .04)
        self.assertAlmostEqual(result["by_provider"]["deepseek"]["risk_group_weighted_brier"], .09)
        self.assertEqual(result["by_provider"]["unknown"]["complete_scored_events"], 0)
        self.assertAlmostEqual(result["realized_paper_pnl"], 1.952)

    def test_review_after_resolution_is_not_scored_as_forecast(self) -> None:
        with database(self.config.database) as conn:
            upsert_markets(conn, [self.market])
            outcome = self.reviewed_prediction(.8)
            outcome["as_of"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            outcome["reviewed_at"] = now_iso()
            save_prediction(conn, "101", "rules", "evidence", outcome)
            resolved_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            settle(conn, "101", "YES", "https://www.federalreserve.gov/test", resolved_at)
            result = report(conn)
        self.assertEqual(result["resolved_markets"], 1)
        self.assertEqual(result["reviewed_pre_outcome_markets"], 0)
        self.assertEqual(result["scored_markets"], 0)


if __name__ == "__main__":
    unittest.main()

