from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.public_execution_episode import (
    CLOB, GAMMA, TWAP_SOURCE, VERSION, PublicReader, capture, encoded, freeze,
    initial_packet, receipt_body, replay, replay_directory, sha, terms,
    verify_clock_receipts, write_new)
from polycast.public_clock import clock_intersection

START = 1791374400000
MONO = 10 ** 12
CONDITION = "0x" + "a" * 64


def receipt(url, body, sent, index):
    raw = encoded(body)
    return {"schema_version": 1, "request_number": index, "method": "GET", "authenticated": False,
            "url": url, "http_status": 200, "sent_local_ms": sent, "received_local_ms": sent + 80,
            "sent_monotonic_ns": MONO + (sent - START) * 1000000,
            "received_monotonic_ns": MONO + (sent + 80 - START) * 1000000,
            "raw_text": raw, "raw_sha256": sha(raw)}


def fixture():
    expected = {"market_id": "1", "event_id": "2", "start_at_ms": START, "end_at_ms": START + 300000,
                "up_asset": "11", "down_asset": "12", "rules": "Frozen exact TWAP rules"}
    gamma = {"id": "1", "version": "v1", "conditionId": CONDITION, "clobTokenIds": '["11","12"]',
             "positionIds": ["999", "998"], "outcomes": '["Up","Down"]', "active": True, "closed": False,
             "acceptingOrders": True, "enableOrderBook": True, "description": expected["rules"],
             "endDate": datetime.fromtimestamp((START + 300000) / 1000, timezone.utc).isoformat(),
             "resolutionSource": TWAP_SOURCE, "cryptoMarketConfig": {"id": "btc-5m-twap-60",
             "twapEnabled": True, "twapLookbackSeconds": 60}, "events": [{"id": "2"}],
             "orderPriceMinTickSize": .01, "orderMinSize": 5, "feesEnabled": True,
             "feeSchedule": {"rate": .07, "exponent": 1, "takerOnly": True}, "secondsDelay": None}
    clob = {"c": CONDITION, "v": "v1", "ao": True, "t": [{"t": "11", "o": "Up"}, {"t": "12", "o": "Down"}],
            "mts": .01, "mos": 5, "fd": {"r": .07, "e": 1, "to": True}, "itode": True}
    clock_receipts = [receipt(CLOB + "/time", (START + 119000) // 1000, START + 119000, 1),
                      receipt("https://api.exchange.coinbase.com/time", {"epoch": (START + 119110) / 1000}, START + 119100, 2)]
    samples = []
    for venue, r, source, precision in (("polymarket", clock_receipts[0], START + 119000, 1000),
                                     ("coinbase", clock_receipts[1], (START + 119110) / 1000 * 1000, 1)):
        samples.append({"venue": venue, "source_url": r["url"], "sent_local_ms": r["sent_local_ms"],
            "received_local_ms": r["received_local_ms"], "monotonic_elapsed_ms": 80,
            "server_ms": source, "precision_ms": precision})
    clock = {"schema_version": 1, **clock_intersection(samples), "samples": samples,
             "checked_at_local_ms": START + 119181, "system_clock_changed": False, "signed_time_verified": False}
    clock["clock_id"] = sha(json.dumps(clock, sort_keys=True))

    def book(asset, price, sent):
        return {"market": CONDITION, "asset_id": asset, "timestamp": str(sent - 420),
                "min_order_size": "5", "tick_size": ".01", "asks": [{"price": price, "size": "10"}],
                "bids": [{"price": str(Decimal(price) - Decimal(".01")), "size": "10"}]}

    initial = [receipt(GAMMA + "/markets/1", gamma, START + 120000, 3),
        receipt(GAMMA + "/events/2", {"id":"2","markets":[gamma]}, START + 120100, 4),
        receipt(CLOB + "/clob-markets/" + CONDITION, clob, START + 120200, 5),
        receipt(CLOB + "/book?token_id=11", book("11", ".60", START + 120300), START + 120300, 6),
        receipt(CLOB + "/book?token_id=12", book("12", ".40", START + 120400), START + 120400, 7)]
    packet = initial_packet(expected, clock, initial, Decimal("5"), 250)
    frozen = freeze(packet, START + 120500, MONO + 120500000000)
    later = [receipt(GAMMA + "/markets/1", gamma, START + 120800, 8),
        receipt(GAMMA + "/events/2", {"id":"2","markets":[gamma]}, START + 120900, 9),
        receipt(CLOB + "/clob-markets/" + CONDITION, clob, START + 121000, 10),
        receipt(CLOB + "/book?token_id=11", book("11", ".55", START + 121100), START + 121100, 11),
        receipt(CLOB + "/book?token_id=12", book("12", ".45", START + 121200), START + 121200, 12)]
    return expected, gamma, clob, clock, clock_receipts, initial, frozen, later


def mutate_body(original, change):
    r = deepcopy(original); body = json.loads(r["raw_text"]); change(body)
    r["raw_text"] = encoded(body); r["raw_sha256"] = sha(r["raw_text"])
    return r


class PublicExecutionEpisodeTests(unittest.TestCase):
    def test_original_cap_accepts_improvement_rejects_price_chase_and_counts_both_sides(self):
        *_, frozen, later = fixture()
        result = replay(frozen, later)
        self.assertEqual(result["results"]["UP"]["status"], "snapshot_fillable")
        up = result["results"]["UP"]
        self.assertEqual(up["frozen_limit_price"], "0.60")
        self.assertEqual(up["quote"]["gross_cost"], "2.75")
        self.assertEqual(up["quote"]["estimated_fee"], "0.08662")
        self.assertEqual(up["price_slippage_per_share"], "-0.05")
        self.assertEqual(result["results"]["DOWN"]["status"], "rejected")
        self.assertIn("Full FOK depth", result["results"]["DOWN"]["reason"])
        self.assertFalse(result["live_ready"]); self.assertFalse(result["profitability_credit"])

    def test_decimal_per_level_fee_and_partial_depth_never_credited_as_fok(self):
        *_, frozen, later = fixture()
        later[3] = mutate_body(later[3], lambda b: b.update(asks=[{"price": ".50", "size": "2"},
            {"price": ".55", "size": "3"}], bids=[{"price": ".49", "size": "10"}]))
        q = replay(frozen, later)["results"]["UP"]["quote"]
        self.assertEqual(q["gross_cost"], "2.65"); self.assertEqual(q["estimated_fee"], "0.08698")
        later[3] = mutate_body(later[3], lambda b: b["asks"].pop())
        self.assertEqual(replay(frozen, later)["results"]["UP"]["status"], "rejected")

    def test_protocol_selected_explicitly_even_when_both_id_fields_populated(self):
        expected, gamma, clob, *_ = fixture()
        self.assertEqual(terms(expected, gamma, clob, {"id":"2","markets":[gamma]})["assets"], ["11", "12"])
        for version in (None, "v2", "v3"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "version"):
                terms(expected, dict(gamma, version=version), clob, {"id":"2","markets":[gamma]})

    def test_fee_scope_exponent_missing_or_changed_rejects_all_later_candidates(self):
        *_, frozen, later = fixture()
        for fee in ({"r": .08, "e": 1, "to": True}, {"r": .07, "e": 2, "to": True},
                    {"r": .07, "e": 1, "to": False}, None):
            changed = deepcopy(later); changed[2] = mutate_body(later[2], lambda b: b.update(fd=fee))
            result = replay(frozen, changed)
            self.assertTrue(all(x["status"] == "rejected" for x in result["results"].values()))

    def test_closed_market_or_contract_revision_invalidates_original_intent(self):
        *_, frozen, later = fixture()
        for update in ({"closed": True}, {"description": "Revised rules"}, {"version": "v2"}):
            changed = deepcopy(later); changed[0] = mutate_body(later[0], lambda b: b.update(update))
            self.assertTrue(all(x["status"] == "rejected" for x in replay(frozen, changed)["results"].values()))
        changed = deepcopy(later); changed[1] = mutate_body(later[1], lambda b: b.update(id="3"))
        self.assertTrue(all(x["status"] == "rejected" for x in replay(frozen, changed)["results"].values()))

    def test_explicit_minimum_unit_conflict_is_never_reported_as_order_permission(self):
        *_, frozen, later = fixture()
        packet = json.loads(frozen["packet_json"])
        self.assertEqual(packet["terms"]["minimum_unit_status"], "gamma_documentation_conflict")
        quote = replay(frozen, later)["results"]["UP"]["quote"]
        self.assertFalse(quote["gamma_notional_minimum_met"])
        self.assertFalse(quote["exchange_order_eligibility_verified"])

    def test_metadata_and_book_constraints_and_identity_are_bound(self):
        *_, frozen, later = fixture()
        for update in ({"market": "0x" + "b" * 64}, {"asset_id": "12"}, {"min_order_size": "6"},
                       {"tick_size": ".005"}, {"asks": [{"price": ".551", "size": "10"}]}):
            changed = deepcopy(later); changed[3] = mutate_body(later[3], lambda b: b.update(update))
            self.assertEqual(replay(frozen, changed)["results"]["UP"]["status"], "rejected")

    def test_one_sided_crossed_duplicate_and_nonfinite_depth_have_no_fill_credit(self):
        *_, frozen, later = fixture()
        for update in ({"bids": []}, {"bids": [{"price": ".56", "size": "1"}]},
                       {"asks": [{"price": ".55", "size": "1"}] * 2},
                       {"asks": [{"price": "NaN", "size": "10"}]}, {"asks": [None]}):
            changed = deepcopy(later); changed[3] = mutate_body(later[3], lambda b: b.update(update))
            self.assertEqual(replay(frozen, changed)["results"]["UP"]["status"], "rejected")

    def test_original_quote_failure_remains_in_denominator_after_later_recovery(self):
        expected, _, _, clock, _, initial, _, later = fixture()
        initial[3] = mutate_body(initial[3], lambda b: b.update(asks=[]))
        frozen = freeze(initial_packet(expected, clock, initial, Decimal("5"), 250), START + 120500, MONO + 120500000000)
        self.assertIn("Initial quote unavailable", replay(frozen, later)["results"]["UP"]["reason"])

    def test_preseal_and_short_delay_requests_cannot_replay(self):
        *_, frozen, later = fixture()
        changed = deepcopy(later); changed[0]["sent_monotonic_ns"] = frozen["sealed_monotonic_ns"] + 249000000
        with self.assertRaisesRegex(ValueError, "delay"):
            replay(frozen, changed)

    def test_future_stale_uncertain_source_or_local_clock_step_rejects(self):
        *_, frozen, later = fixture()
        for timestamp in (START + 121180, START + 110000, START + 121120):
            changed = deepcopy(later); changed[3] = mutate_body(later[3], lambda b: b.update(timestamp=str(timestamp)))
            self.assertEqual(replay(frozen, changed)["results"]["UP"]["status"], "rejected")
        changed = deepcopy(later); changed[3]["received_local_ms"] += 200
        self.assertIn("clock step", replay(frozen, changed)["results"]["UP"]["reason"])

    def test_original_clock_source_and_estimate_must_match(self):
        _, _, _, clock, clock_receipts, *_ = fixture()
        verify_clock_receipts(clock, clock_receipts)
        changed = deepcopy(clock); changed["offset_ms"] += 1
        with self.assertRaisesRegex(ValueError, "interval"):
            verify_clock_receipts(changed, clock_receipts)
        modified = deepcopy(clock_receipts); modified[1] = mutate_body(modified[1], lambda b: b.update(epoch=b["epoch"] + 1))
        with self.assertRaisesRegex(ValueError, "original"):
            verify_clock_receipts(clock, modified)

    def test_hash_tampering_and_nonpublic_or_authenticated_receipts_fail(self):
        *_, frozen, later = fixture()
        changed = deepcopy(frozen); changed["packet_json"] += " "
        with self.assertRaisesRegex(ValueError, "integrity"):
            replay(changed, later)
        for update in ({"authenticated": True}, {"method": "POST"}, {"url": CLOB + "/orders"}, {"raw_sha256": "0" * 64}):
            r = dict(later[3], **update)
            with self.assertRaises(ValueError):
                receipt_body(r, later[3]["url"])

    def test_read_only_file_replay_checks_original_manifest_and_receipts(self):
        _, _, _, _, clock_receipts, initial, frozen, later = fixture()
        result = replay(frozen, later)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp); receipts = clock_receipts + initial + later
            write_new(directory / "frozen-intent.json", frozen); write_new(directory / "replay.json", result)
            files = []
            for i, r in enumerate(receipts, 1):
                p = directory / f"receipt-{i:02}.json"; write_new(p, r)
                files.append({"filename": p.name, "sha256": sha(p.read_bytes())})
            write_new(directory / "capture.json", {"version": VERSION, "status": "capture_completed",
                "requests": 12, "receipt_files": files, "later_request_numbers": [8, 9, 10, 11, 12], "summary": result})
            with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")):
                self.assertEqual(replay_directory(directory), result)
                p = directory / "receipt-11.json"; p.write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "artifact hash"):
                    replay_directory(directory)

    def test_capture_repeat_and_nonallowlisted_get_do_not_access_network(self):
        with tempfile.TemporaryDirectory() as tmp, patch("urllib.request.OpenerDirector.open") as network:
            with self.assertRaises(FileExistsError):
                capture(Path(tmp) / "missing-selection.json", Path(tmp), Decimal("5"), 250)
            reader = PublicReader(Path(tmp))
            for url in (CLOB + "/order", CLOB + "/book?token_id=11&key=x", "https://example.com/time"):
                with self.assertRaisesRegex(ValueError, "allowlist"):
                    reader.get(url)
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
