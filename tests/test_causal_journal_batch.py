from __future__ import annotations
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.journal_replay import verify_causal_batch, verify_causal_journal, segment_records
from polycast.microstructure_archive import archive_segment
from tests.test_causal_features import fixture


def journal_rows(snapshot):
    rows = {}
    pm = {row['input_snapshot_record_seq']: row for row in snapshot['causal_context']['polymarket_up']}
    for sample in snapshot['causal_context']['coinbase']:
        base = {key: copy.deepcopy(value) for key, value in snapshot.items()
                if key not in ('causal_context', 'causal_complete', 'base_snapshot_record_seq')}
        base.update(schema_version=1, feature_version='btc5m-public-microstructure-v2-clock-checked', record_seq=sample['input_snapshot_record_seq'])
        for key in ('received_at_ms', 'monotonic_ns', 'as_of_utc_ms', 'clock_reference'):
            base[key] = sample[key]
        base['coinbase']['book'].update(midpoint=sample['midpoint'], received_at_ms=sample['book_received_at_ms'], source_timestamp_ms=sample['source_timestamp_ms'])
        if sample['input_snapshot_record_seq'] in pm:
            point = pm[sample['input_snapshot_record_seq']]
            base['polymarket']['up'].update(midpoint=point['midpoint'], received_at_ms=point['book_received_at_ms'], source_timestamp_ms=point['source_timestamp_ms'])
        rows[base['record_seq']] = base
    for sample in snapshot['causal_context']['reference']['points']:
        rows[sample['journal_seq']] = {'schema_version': 1, 'session_id': snapshot['session_id'], 'record_seq': sample['journal_seq'],
            'kind': 'reference_observation', 'raw_text': sample['raw_text'], 'raw_sha256': sample['raw_sha256'],
            'received_at_ms': sample['first_read_at_ms'], 'monotonic_ns': sample['first_read_monotonic_ns'],
            'source_timestamp_ms': sample['point']['timestamp_ms']}
    base = {key: copy.deepcopy(value) for key, value in snapshot.items()
            if key not in ('causal_context', 'causal_complete', 'base_snapshot_record_seq')}
    base.update(schema_version=1, record_seq=snapshot['base_snapshot_record_seq'], feature_version='btc5m-public-microstructure-v2-clock-checked')
    rows[base['record_seq']] = base
    rows[snapshot['record_seq']] = {**copy.deepcopy(snapshot), 'schema_version': 1}
    return rows


class CausalBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.first = fixture()
        self.second = copy.deepcopy(self.first)
        self.second['record_seq'] = 302
        self.rows = journal_rows(self.first)
        self.rows[302] = {**copy.deepcopy(self.second), 'schema_version': 1}
        self.publish()

    def publish(self, first=1, last=302, number=0):
        name = f'raw-fixture-{number:05d}.jsonl'
        raw = b''.join((json.dumps(self.rows.get(seq, {'schema_version': 1, 'session_id': 'fixture', 'record_seq': seq, 'kind': 'filler'})) + '\n').encode()
                       for seq in range(first, last + 1))
        (self.directory / name).write_bytes(raw)
        seal = self.directory / (name + '.seal.json')
        seal.write_text(json.dumps({'schema_version': 1, 'filename': name, 'session_id': 'fixture', 'bytes': len(raw),
                                   'sha256': hashlib.sha256(raw).hexdigest(), 'first_record_seq': first, 'last_record_seq': last}))
        return seal

    def batch(self, second=None):
        return verify_causal_batch(self.directory, [(self.first, 'first-digest'), (second or self.second, 'second-digest')])

    def test_shared_gzip_segment_decodes_once_and_matches_individual_reports(self):
        archive_segment(self.directory, self.directory / 'raw-fixture-00000.jsonl.seal.json')
        expected = [verify_causal_journal(self.directory, snapshot, digest)
                    for snapshot, digest in ((self.first, 'first-digest'), (self.second, 'second-digest'))]
        with patch('experiments.journal_replay.segment_records', wraps=segment_records) as read:
            result = self.batch()
        self.assertEqual(result['results'], expected)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(result['diagnostics']['segments_decoded'], 1)
        self.assertEqual(result['diagnostics']['source_records_decoded'], 302)
        self.assertLess(result['diagnostics']['source_records_retained'], 302)

    def test_byte_corruption_rejects_all_consumers_without_crediting_a_segment(self):
        path = self.directory / 'raw-fixture-00000.jsonl'
        path.write_bytes(path.read_bytes() + b'changed')
        result = self.batch()
        self.assertTrue(all(row['status'] == 'rejected' for row in result['results']))
        self.assertEqual(result['diagnostics']['segments_decoded'], 0)

    def test_a_self_consistent_rewritten_freeze_still_fails_original_price_receipt_check(self):
        changed = copy.deepcopy(self.second)
        changed['causal_context']['coinbase'][10]['midpoint'] *= 1.01
        self.rows[302] = {**changed, 'schema_version': 1}
        self.publish()
        result = self.batch(changed)
        self.assertEqual(result['results'][0]['status'], 'verified_causal_context')
        self.assertIn('history changed', result['results'][1]['error'])

    def test_future_reference_and_invalid_session_are_rejected_before_decoding(self):
        for key in ('future', 'session', 'boolean'):
            changed = copy.deepcopy(self.first)
            if key == 'future': changed['causal_context']['coinbase'][0]['input_snapshot_record_seq'] = 999
            elif key == 'session': changed['session_id'] = '../fixture'
            else: changed['causal_context']['coinbase'][0]['input_snapshot_record_seq'] = True
            result = verify_causal_batch(self.directory, [(changed, 'changed')])
            self.assertEqual(result['results'][0]['status'], 'rejected')
            self.assertEqual(result['diagnostics']['segments_decoded'], 0)

    def test_unsealed_tail_is_pending_without_discarding_an_earlier_verified_decision(self):
        pending = copy.deepcopy(self.second)
        pending['record_seq'] = 500
        (self.directory / 'raw-fixture-00001.jsonl').write_text(json.dumps({**pending, 'schema_version': 1}) + '\n')
        result = self.batch(pending)
        self.assertEqual(result['results'][0]['status'], 'verified_causal_context')
        self.assertIn('not yet sealed', result['results'][1]['error'])

    def test_missing_prefix_segment_rejects_only_decisions_that_cross_that_gap(self):
        self.publish(last=301)
        self.publish(first=302, last=302, number=2)
        result = self.batch()
        self.assertEqual(result['results'][0]['status'], 'verified_causal_context')
        self.assertIn('Missing V3 source journal prefix', result['results'][1]['error'])

    def test_changed_seal_cannot_publish_an_earlier_cached_result(self):
        from experiments.journal_replay import read_seal
        calls = 0
        def replace_before_second_read(directory, path):
            nonlocal calls
            calls += 1
            # First decision: header, segment read, post-read check. Next header changes.
            if calls == 4:
                seal = json.loads(path.read_text())
                seal['closed_at_ms'] = 123
                path.write_text(json.dumps(seal))
            return read_seal(directory, path)
        with patch('experiments.journal_replay.read_seal', side_effect=replace_before_second_read):
            result = self.batch()
        self.assertTrue(all(row['status'] == 'rejected' for row in result['results']))
        self.assertTrue(all('seal changed' in row['error'] for row in result['results']))


if __name__ == '__main__':
    unittest.main()
