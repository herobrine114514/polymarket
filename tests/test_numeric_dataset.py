from __future__ import annotations
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from experiments.numeric_dataset import assemble, read_decision
from polycast.store import database, upsert_markets


class NumericDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / "decisions").mkdir()
        self.start = int(datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp() * 1000)
        now = self.start + 120000
        book = {"bid": .4, "ask": .5, "midpoint": .45, "spread": .1,
                "top5_imbalance": 0, "received_at_ms": now - 1000, "source_timestamp_ms": now - 2000}
        self.snapshot = {"kind": "feature_snapshot", "feature_version": "btc5m-public-microstructure-v1",
                         "received_at_ms": now, "paired": True,
                         "market": {"market_id": "1", "event_id": "2", "start_at_ms": self.start,
                                    "end_at_ms": self.start + 300000, "up_asset": "100", "down_asset": "200", "rules": "rules"},
                         "polymarket": {"up": book.copy(), "down": book.copy()},
                         "coinbase": {"book": book.copy(), "flow": {key: {"complete": True, "taker_imbalance": 0}
                             for key in ("10s", "30s")}}}
        self.path = self.directory / "decisions/event-1.json"
        self.write(self.snapshot)

    def write(self, snapshot):
        raw = json.dumps(snapshot)
        self.path.write_text(json.dumps({"schema_version": 1,
            "decision_protocol": "start-plus-120s-tolerance-3s-v1",
            "snapshot_json": raw, "snapshot_sha256": hashlib.sha256(raw.encode()).hexdigest()}), encoding="utf-8")

    def populate(self, conn):
        market = {"id": "1", "event_id": "2", "event_title": "BTC", "event_slug": "btc-updown-5m-1",
                  "question": "Up?", "description": "rules", "end_at": "2026-10-07T00:05:00Z",
                  "yes_token": "100", "no_token": "200", "liquidity": 10000, "volume_24h": 1000,
                  "min_order_size": 5, "tick_size": .01, "fee_rate": .02, "category": "crypto5m"}
        upsert_markets(conn, [market])
        url = "https://gamma-api.polymarket.com/events/slug/btc-updown-5m-1"
        conn.execute("INSERT INTO resolutions VALUES(?,?,?,?,?)", ("1", "YES", url,
                     "2026-10-07T00:07:00Z", "2026-10-07T00:10:00Z"))
        conn.execute("INSERT INTO settlement_checks VALUES(?,?,?,?,?,?)", ("1", "2026-10-07T00:10:00Z",
                     "verified", "sources_agree", url, "YES"))

    def test_delayed_official_label_is_unavailable_until_its_actual_observation(self):
        with database(self.directory / "db.sqlite3") as conn:
            self.populate(conn)
            pending = assemble(self.directory, conn, self.start + 9 * 60000)
            self.assertEqual(pending["events"], [])
            result = assemble(self.directory, conn, self.start + 11 * 60000)
            self.assertEqual(len(result["events"]), 1)
            self.assertEqual(result["events"][0]["outcome"], 1)
            self.assertEqual(result["events"][0]["label_observed_at_ms"], self.start + 10 * 60000)
            self.assertFalse(result["profitability_validated"])

    def test_contract_revision_or_unverified_official_result_never_becomes_a_label(self):
        with database(self.directory / "db.sqlite3") as conn:
            self.populate(conn)
            conn.execute("UPDATE markets SET description='revised'")
            result = assemble(self.directory, conn, self.start + 11 * 60000)
            self.assertEqual(result["events"], [])
            self.assertEqual(result["exclusions"]["official_label_or_contract_not_verified"], 1)
            conn.execute("UPDATE markets SET description='rules'")
            conn.execute("UPDATE settlement_checks SET status='pending'")
            self.assertEqual(assemble(self.directory, conn, self.start + 11 * 60000)["events"], [])

    def test_hash_mismatch_and_future_book_are_rejected(self):
        value = json.loads(self.path.read_text(encoding="utf-8"))
        value["snapshot_json"] += " "
        self.path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_decision(self.path)
        self.snapshot["polymarket"]["up"]["source_timestamp_ms"] = self.start + 121000
        self.write(self.snapshot)
        with database(self.directory / "db.sqlite3") as conn:
            self.populate(conn)
            result = assemble(self.directory, conn, self.start + 11 * 60000)
            self.assertEqual(result["events"], [])
            self.assertEqual(result["exclusions"]["incomplete_or_invalid_frozen_features"], 1)

    def test_old_local_clock_and_new_checked_utc_versions_cannot_be_mixed(self):
        new = json.loads(json.dumps(self.snapshot))
        new["feature_version"] = "btc5m-public-microstructure-v2-clock-checked"
        new["market"].update(market_id="3", event_id="4")
        new["as_of_utc_ms"] = new["received_at_ms"]
        new["clock_reference"] = {"status": "venues_agree", "checked_at_local_ms": new["received_at_ms"] - 1000,
                                   "offset_ms": 0, "uncertainty_ms": 100}
        raw = json.dumps(new)
        (self.directory / "decisions/event-3.json").write_text(json.dumps({"schema_version": 1,
            "decision_protocol": "utc-start-plus-120s-tolerance-3s-v2", "snapshot_json": raw,
            "snapshot_sha256": hashlib.sha256(raw.encode()).hexdigest()}), encoding="utf-8")
        with database(self.directory / "db.sqlite3") as conn:
            self.populate(conn)
            with self.assertRaisesRegex(ValueError, "Mixed capture"):
                assemble(self.directory, conn, self.start + 11 * 60000)
            result = assemble(self.directory, conn, self.start + 11 * 60000,
                              feature_version="btc5m-public-microstructure-v2-clock-checked")
            self.assertEqual(result["events"], [])
            self.assertEqual(result["coverage"]["pending_or_missing_labels"], 1)
            self.assertEqual(result["exclusions"]["different_feature_version"], 1)
            self.assertEqual(result["feature_versions"], [new["feature_version"]])

    def test_causal_journal_audit_joins_only_an_original_verified_freeze_to_its_label(self):
        from tests.test_causal_features import fixture
        from tests.test_causal_journal_batch import journal_rows
        snapshot = fixture()
        snapshot['market'].update(up_asset='100', down_asset='200', rules='rules')
        folder = self.directory/'decisions-v3'; folder.mkdir()
        raw = json.dumps(snapshot)
        (folder/'event-1.json').write_text(json.dumps({'schema_version': 1,
            'decision_protocol': 'utc-start-plus-120s-tolerance-3s-v3', 'snapshot_json': raw,
            'snapshot_sha256': hashlib.sha256(raw.encode()).hexdigest()}), encoding='utf-8')
        rows = journal_rows(snapshot)
        name = 'raw-fixture-00000.jsonl'
        encoded = b''.join((json.dumps(rows.get(seq, {'schema_version': 1, 'session_id': 'fixture', 'record_seq': seq, 'kind': 'filler'}))+'\n').encode() for seq in range(1,302))
        (self.directory/name).write_bytes(encoded)
        (self.directory/(name+'.seal.json')).write_text(json.dumps({'schema_version': 1, 'filename': name,
            'session_id': 'fixture', 'bytes': len(encoded), 'sha256': hashlib.sha256(encoded).hexdigest(),
            'first_record_seq': 1, 'last_record_seq': 301}), encoding='utf-8')
        with database(self.directory/'db.sqlite3') as conn:
            self.populate(conn)
            result = assemble(self.directory, conn, self.start+11*60000, feature_version='btc5m-causal-features-v3')
            self.assertEqual(len(result['events']), 1)
            self.assertEqual(len(result['events'][0]['features']), 32)
            self.assertEqual(result['journal_audit']['verified_decisions'], 1)
            (self.directory/name).write_bytes(encoded+b'tampered')
            rejected = assemble(self.directory, conn, self.start+11*60000, feature_version='btc5m-causal-features-v3')
            self.assertEqual(rejected['events'], [])
            self.assertEqual(rejected['journal_audit']['rejected_decisions'], 1)
            self.assertEqual(rejected['coverage']['complete_features'], 0)


if __name__ == "__main__":
    unittest.main()
