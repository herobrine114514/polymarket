from __future__ import annotations
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from polycast.microstructure_archive import archive_one, archive_segment, seal_closed_session, ArchiveFailure


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.name = "raw-session-00000.jsonl"
        self.content = b'{"session_id":"session","record_seq":1,"raw_text":"public"}\n' * 2000
        (self.root / self.name).write_bytes(self.content)
        self.seal = self.root / (self.name + ".seal.json")
        self.seal.write_text(json.dumps({"schema_version": 1, "filename": self.name, "session_id": "session",
            "bytes": len(self.content), "sha256": hashlib.sha256(self.content).hexdigest(),
            "first_record_seq": 1, "last_record_seq": 2000}), encoding="utf-8")

    def test_published_gzip_restores_every_byte_before_original_is_retired(self):
        result = archive_segment(self.root, self.seal)
        self.assertFalse((self.root / self.name).exists())
        self.assertEqual(gzip.decompress((self.root / (self.name + ".gz")).read_bytes()), self.content)
        self.assertGreater(result["bytes_saved"], 0)
        self.assertTrue((self.root / (self.name + ".archive.json")).exists())
        self.assertIsNone(archive_one(self.root))

    def test_active_segment_or_wrong_seal_never_removes_original(self):
        self.assertIsNone(archive_segment(self.root, self.seal, active_name=self.name))
        record = json.loads(self.seal.read_text(encoding="utf-8"))
        record["sha256"] = "0" * 64
        self.seal.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ArchiveFailure):
            archive_one(self.root)
        self.assertEqual((self.root / self.name).read_bytes(), self.content)
        self.assertFalse((self.root / (self.name + ".gz")).exists())

    def test_wrong_gzip_or_conflicting_index_keeps_original(self):
        output = self.root / (self.name + ".gz")
        output.write_bytes(gzip.compress(b"changed"))
        with self.assertRaises(ValueError):
            archive_segment(self.root, self.seal)
        self.assertEqual((self.root / self.name).read_bytes(), self.content)
        output.write_bytes(gzip.compress(self.content))
        index = self.root / (self.name + ".archive.json")
        index.write_text('{"raw_sha256":"different"}')
        with self.assertRaises(ValueError):
            archive_segment(self.root, self.seal)
        self.assertTrue((self.root / self.name).exists())

    def test_recovering_after_gzip_publication_validates_the_original_again(self):
        (self.root / (self.name + ".gz")).write_bytes(gzip.compress(self.content))
        result = archive_segment(self.root, self.seal)
        self.assertEqual(result["raw_sha256"], hashlib.sha256(self.content).hexdigest())
        self.assertFalse((self.root / self.name).exists())

    def test_seal_cannot_reference_another_directory_or_filename(self):
        record = json.loads(self.seal.read_text(encoding="utf-8"))
        record["filename"] = "../outside.jsonl"
        self.seal.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            archive_segment(self.root, self.seal)
        self.assertTrue((self.root / self.name).exists())

    def test_explicit_closed_session_migration_requires_complete_dense_records(self):
        first = self.root / "raw-old-00000.jsonl"
        original = b'{"session_id":"old","record_seq":1}\n{"session_id":"old","record_seq":2}\n'
        first.write_bytes(original)
        self.assertEqual(seal_closed_session(self.root, "old"), [first.name])
        self.assertEqual(seal_closed_session(self.root, "old"), [])
        second = self.root / "raw-broken-00000.jsonl"
        second.write_bytes(b'{"session_id":"broken","record_seq":1}')
        with self.assertRaises(ValueError):
            seal_closed_session(self.root, "broken")
        self.assertTrue(second.exists())
        self.assertFalse((self.root / (second.name + ".seal.json")).exists())


if __name__ == "__main__":
    unittest.main()
