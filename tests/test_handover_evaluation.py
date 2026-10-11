from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest

from experiments.analyze_edge_improvement import qualify, metrics
from polycast.research_shadow import ResearchShadow, shadow_status


class FrozenEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE evidence(id INTEGER, fetched_at TEXT)")
        self.conn.execute("INSERT INTO evidence VALUES(1,'2026-10-03T15:01:00Z')")
        at = "2026-10-03T15:01:00Z"
        prediction = {"market_id": "1", "as_of": at, "p_yes": .7, "raw_p_yes": .8,
                      "abstain": False, "supporting_evidence_ids": [1], "counter_evidence_ids": []}
        output = {**prediction, "analysis_abstain": False, "review": {"approved": True},
                  "reviewed_at": "2026-10-03T15:01:10Z", "model_version": "deepseek-v4-pro",
                  "prompt_version": "btc5m-v7-causal-fast-review-unvalidated-twap-proxy"}
        evidence = {"id": 1, "published_at": at, "first_seen_at": at, "source_updated_at": at}
        payload = {"market_id": "1", "as_of": at, "category": "crypto5m",
                   "end_at": "2026-10-03T15:05:00Z", "evidence": [evidence]}
        self.row = {"id": 1, "market_id": "1", "event_id": "event", "as_of": at,
                    "abstain": 0, "probability": .7, "end_at": payload["end_at"],
                    "draft_count": 1, "event_markets": 1, "model_output_json": json.dumps(output),
                    "payload_json": json.dumps(payload), "result_json": json.dumps(prediction),
                    "draft_created_at": "2026-10-03T15:01:09Z", "winning_side": "YES",
                    "resolved_at": "2026-10-03T15:05:00Z", "recorded_at": "2026-10-03T15:05:30Z",
                    "source_url": "https://gamma-api.polymarket.com/markets/1",
                    "book_bid": .4, "book_ask": .6, "book_mid": .5, "staged_bid": .4, "staged_ask": .6,
                    "captured_at": at, "book_timestamp": at, "staged_at": at, "staged_book_at": at}
        self.cutoff = datetime(2026, 10, 3, 15, 6, tzinfo=timezone.utc)

    def evaluate(self, row=None):
        return qualify(row or self.row, self.conn, self.cutoff)

    def test_stored_effective_and_raw_probability_remain_distinct(self):
        item, reason = self.evaluate()
        self.assertIsNone(reason)
        self.assertEqual((item['raw'],item['effective'],item['market']),(.8,.7,.5))
        self.assertAlmostEqual(metrics([item], 'effective')['brier'], .09)

    def test_review_rejection_never_receives_validation_credit(self):
        row = deepcopy(self.row)
        output = json.loads(row['model_output_json'])
        output['review']['approved'] = False
        row['model_output_json'] = json.dumps(output)
        self.assertEqual(self.evaluate(row)[1], 'review_rejected_or_missing')

    def test_publication_after_end_or_missing_resolution_excluded(self):
        for timestamp in ('2026-10-03T15:05:00Z','2026-10-03T15:05:01Z',None):
            row = deepcopy(self.row)
            output = json.loads(row['model_output_json'])
            output['reviewed_at'] = timestamp
            row['model_output_json'] = json.dumps(output)
            with self.subTest(timestamp=timestamp):
                self.assertIsNone(self.evaluate(row)[0])

    def test_exact_draft_identity_and_original_probability_required(self):
        for field, value in (('as_of','2026-10-03T15:02:00Z'),('p_yes',.9),('raw_p_yes',.9)):
            row = deepcopy(self.row)
            draft = json.loads(row['result_json'])
            draft[field] = value
            row['result_json'] = json.dumps(draft)
            with self.subTest(field=field):
                self.assertIsNone(self.evaluate(row)[0])

    def test_evidence_first_available_after_forecast_excluded(self):
        row = deepcopy(self.row)
        payload = json.loads(row['payload_json'])
        payload['evidence'][0]['first_seen_at'] = '2026-10-03T15:01:01Z'
        row['payload_json'] = json.dumps(payload)
        self.assertEqual(self.evaluate(row)[1], 'evidence_unavailable_at_as_of')
        self.conn.execute("UPDATE evidence SET fetched_at='2026-10-03T15:01:01Z'")
        self.assertEqual(self.evaluate()[1], 'evidence_unavailable_at_as_of')

    def test_stale_one_sided_or_changed_baseline_not_paired(self):
        for updates in ({'book_bid':None}, {'book_timestamp':'2026-10-03T15:00:49Z'},
                        {'staged_bid':.3}, {'book_mid':.6}):
            with self.subTest(updates=updates):
                self.assertEqual(self.evaluate({**self.row,**updates})[1], 'no_identical_fresh_two_sided_prior')

    def test_no_duplicate_drafts_or_multi_market_events(self):
        for field in ('draft_count','event_markets'):
            with self.subTest(field=field):
                self.assertEqual(self.evaluate({**self.row,field:2})[1],'ambiguous_frozen_forecast_or_event')


class RegisteredStudyClosureTests(unittest.TestCase):
    def test_closed_study_never_launches_worker_and_preserves_original_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            study = root/'calibration-prospective-v1'
            study.mkdir()
            original = '{"status":"error"}'
            (study/'status.json').write_text(original, encoding='utf-8')
            (study/'closed.json').write_text('{}', encoding='utf-8')
            worker = ResearchShadow(root,root,root/'test.sqlite3')
            worker._watch()
            self.assertIsNone(worker._process)
            self.assertEqual(shadow_status(root)['status'],'closed')
            self.assertEqual((study/'status.json').read_text(encoding='utf-8'),original)


if __name__ == '__main__':
    unittest.main()
