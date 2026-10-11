from __future__ import annotations
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from experiments.journal_replay import segment_records, session_records, verify_frozen_journal
from polycast.microstructure_archive import archive_segment


class JournalReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def segment(self, number=0, first=1, values=None):
        name = f"raw-session-{number:05d}.jsonl"
        values = values or [{"kind": "raw_frame"}, {"kind": "feature_snapshot"}]
        rows = [{**value, "schema_version": 1, "session_id": "session", "record_seq": first + i}
                for i, value in enumerate(values)]
        raw = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
        (self.directory / name).write_bytes(raw)
        seal = self.directory / (name + ".seal.json")
        seal.write_text(json.dumps({"schema_version": 1, "filename": name, "session_id": "session",
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "first_record_seq": first, "last_record_seq": first + len(rows) - 1}), encoding="utf-8")
        return seal, rows

    def test_raw_and_verified_gzip_replay_have_identical_records(self):
        seal, rows = self.segment()
        self.assertEqual(list(segment_records(self.directory, seal)), rows)
        archive_segment(self.directory, seal)
        self.assertFalse((self.directory / "raw-session-00000.jsonl").exists())
        self.assertEqual(list(session_records(self.directory, "session")), rows)

    def test_modified_archive_or_sequence_fails_before_first_record_is_exposed(self):
        seal, rows = self.segment()
        archive_segment(self.directory, seal)
        compressed = self.directory / "raw-session-00000.jsonl.gz"
        compressed.write_bytes(compressed.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "Compressed archive"):
            next(segment_records(self.directory, seal))
        seal, rows = self.segment(number=1, first=3)
        path = self.directory / "raw-session-00001.jsonl"
        rows[1]["record_seq"] = 9
        raw = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
        path.write_bytes(raw)
        metadata = json.loads(seal.read_text(encoding="utf-8"))
        metadata.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        seal.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "identity/sequence"):
            next(segment_records(self.directory, seal))

    def test_missing_session_segment_and_unsealed_files_are_not_complete_replay(self):
        self.segment()
        self.segment(number=2, first=3)
        with self.assertRaisesRegex(ValueError, "Missing or reordered"):
            next(session_records(self.directory, "session"))
        with self.assertRaises(ValueError):
            list(session_records(self.directory, "../session"))

    def test_frozen_feature_must_match_its_immutable_raw_journal_record(self):
        snapshot = {"kind": "feature_snapshot", "feature_version": "btc5m-public-microstructure-v1",
                    "market": {"market_id": "1"}, "received_at_ms": 120000}
        seal, rows = self.segment(values=[snapshot])
        original = {key: value for key, value in rows[0].items() if key != "schema_version"}
        decision = self.directory / "event-1.json"

        def write(value):
            raw = json.dumps(value)
            decision.write_text(json.dumps({"schema_version": 1, "decision_protocol": "start-plus-120s-tolerance-3s-v1",
                "snapshot_json": raw, "snapshot_sha256": hashlib.sha256(raw.encode()).hexdigest()}), encoding="utf-8")
        write(original)
        archive_segment(self.directory, seal)
        self.assertEqual(verify_frozen_journal(self.directory, decision)["status"], "verified")
        write({**original, "received_at_ms": 120001})
        with self.assertRaisesRegex(ValueError, "differs from verified"):
            verify_frozen_journal(self.directory, decision)


if __name__ == "__main__":
    unittest.main()
