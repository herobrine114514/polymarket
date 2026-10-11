from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from polycast.analysis import _run_deepseek
from polycast.billing import capture_usage, cost_summary, deepseek_usage, emit_deepseek_usage, save_model_usage
from polycast.cli import _budgeted_model_call
from polycast.config import Config
from polycast.store import database, reserve_model_call


def response(content='{"ok":true}', **changes):
    result = {"model": "deepseek-v4-pro", "usage": {
        "prompt_tokens": 160, "completion_tokens": 40, "total_tokens": 200,
        "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 96,
        "completion_tokens_details": {"reasoning_tokens": 20}},
        "choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    result.update(changes)
    return result


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(data_dir=self.root)
        with database(self.config.database) as conn:
            conn.execute("""INSERT INTO markets(id,event_id,event_title,event_slug,question,
                description,end_at,yes_token,no_token,liquidity,volume_24h,min_order_size,
                tick_size,fee_rate,discovered_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("1", "event", "event", "event", "question", "rules", "2099-01-01T00:00:00+00:00",
                 "yes", "no", 10000, 10000, 5, .01, .04, "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))

    def tearDown(self):
        self.temp.cleanup()

    def test_cache_rates_and_reasoning_are_not_double_charged(self):
        item = deepseek_usage(response(), "deepseek-v4-pro")
        self.assertEqual(item['usage_status'], 'measured')
        self.assertEqual(item['cost_lower_usd'], '0.000143968')
        self.assertEqual(item['cost_upper_usd'], '0.000287936')
        self.assertFalse(item['pricing_snapshot']['invoice_reconciled'])

    def test_missing_invalid_and_inconsistent_counts_remain_unpriced(self):
        for usage in [{}, {**response()['usage'], 'prompt_tokens': True},
                      {**response()['usage'], 'total_tokens': 201},
                      {**response()['usage'], 'prompt_cache_hit_tokens': -1},
                      {**response()['usage'], 'completion_tokens': 10**20},
                      {**response()['usage'], 'completion_tokens_details': {'reasoning_tokens': 41}}]:
            with self.subTest(usage=usage):
                item = deepseek_usage(response(usage=usage), "deepseek-v4-pro")
                self.assertNotEqual(item['usage_status'], 'measured')
                self.assertIsNone(item['cost_lower_usd'])

    def test_model_mismatch_unrecognized_metadata_and_extra_secrets_are_not_priced_or_saved(self):
        for model in ['deepseek-flash', 'UNTRUSTED_SECRET', ['invalid'], {'bad': True}, None]:
            item = deepseek_usage(response(model=model, authorization='TEST_SECRET'), 'deepseek-v4-pro')
            self.assertIsNone(item['cost_lower_usd'])
            self.assertNotIn('TEST_SECRET', json.dumps(item))
            self.assertNotIn('UNTRUSTED_SECRET', json.dumps(item))

    def test_schema_failure_still_persists_billable_usage_and_failed_call(self):
        raw = response('{"ok":true,"secret":"PRIVATE_MODEL_CONTENT"}')
        with patch('polycast.analysis.deepseek_key', return_value='PRIVATE_KEY'), \
                patch('polycast.analysis.DEEPSEEK_MODEL', 'deepseek-v4-pro'), \
                patch('polycast.analysis.urlopen'), patch('polycast.analysis.json.load', return_value=raw):
            with self.assertRaises(RuntimeError):
                _budgeted_model_call(self.config, '1', 'review',
                    lambda: _run_deepseek('PRIVATE_PROMPT', SCHEMA, 2, self.root), 'deepseek')
        with database(self.config.database) as conn:
            summary = cost_summary(conn)
            row = conn.execute('SELECT status FROM model_runs').fetchone()
            receipt = conn.execute('SELECT receipt_json FROM model_usage').fetchone()[0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(summary['priced_calls'], 1)
        for secret in ('PRIVATE_KEY', 'PRIVATE_PROMPT', 'PRIVATE_MODEL_CONTENT'):
            self.assertNotIn(secret, receipt)

    def test_invalid_json_still_records_usage_before_parsing(self):
        captured = []
        with capture_usage(captured.append), patch('polycast.analysis.deepseek_key', return_value='key'), \
                patch('polycast.analysis.urlopen'), patch('polycast.analysis.json.load', return_value=response('bad-json')):
            with self.assertRaises(RuntimeError):
                _run_deepseek('prompt', SCHEMA, 2, self.root)
        self.assertEqual(captured[0]['usage_status'], 'measured')

    def test_unknown_historical_codex_and_timeout_costs_prevent_complete_coverage(self):
        with database(self.config.database) as conn:
            reserve_model_call(conn, '1', 'analysis', 'gpt-6-astra', 10, 'codex')
            reserve_model_call(conn, '1', 'analysis', 'deepseek-v4-pro', 10, 'deepseek')
            priced_id = reserve_model_call(conn, '1', 'review', 'deepseek-v4-pro', 10, 'deepseek')
            save_model_usage(conn, priced_id, deepseek_usage(response(), 'deepseek-v4-pro'))
            summary = cost_summary(conn)
        self.assertEqual(summary['total_calls'], 3)
        self.assertEqual(summary['unpriced_calls'], 2)
        self.assertFalse(summary['model_cost_coverage_complete'])
        self.assertIsNone(summary['all_in_net_pnl'])

    def test_receipt_is_immutable_and_identical_replay_is_idempotent(self):
        with database(self.config.database) as conn:
            call_id = reserve_model_call(conn, '1', 'analysis', 'deepseek-v4-pro', 10, 'deepseek')
            item = deepseek_usage(response(), 'deepseek-v4-pro')
            save_model_usage(conn, call_id, item)
            save_model_usage(conn, call_id, item)
            with self.assertRaises(ValueError):
                save_model_usage(conn, call_id, {**item, 'cost_lower_usd': '0'})
            self.assertEqual(cost_summary(conn)['priced_calls'], 1)

    def test_thread_contexts_are_isolated_and_reset_after_exception(self):
        barrier = threading.Barrier(2)
        captured = [[], []]
        def run(index):
            with capture_usage(captured[index].append):
                barrier.wait(timeout=3)
                emit_deepseek_usage(response(usage={**response()['usage'], 'completion_tokens_details': {'reasoning_tokens': index}}), 'deepseek-v4-pro')
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, [0, 1]))
        self.assertEqual([items[0]['reasoning_tokens'] for items in captured], [0, 1])
        with self.assertRaises(RuntimeError):
            with capture_usage(captured[0].append):
                raise RuntimeError('test context cleanup')
        emit_deepseek_usage(response(), 'deepseek-v4-pro')
        self.assertEqual([len(items) for items in captured], [1, 1])

    def test_real_second_database_connection_can_write_during_network_operation(self):
        def remote_operation():
            second = sqlite3.connect(self.config.database, timeout=.2)
            second.execute("INSERT INTO meta(key,value) VALUES('network_write','ok')")
            second.commit(); second.close()
            emit_deepseek_usage(response(), 'deepseek-v4-pro')
            return {'ok': True}
        self.assertEqual(_budgeted_model_call(self.config, '1', 'analysis', remote_operation, 'deepseek'), {'ok': True})
        with database(self.config.database) as conn:
            self.assertEqual(cost_summary(conn)['priced_calls'], 1)

    def test_empty_account_reports_missing_cost_not_zero(self):
        with database(self.config.database) as conn:
            summary = cost_summary(conn)
        self.assertIsNone(summary['recorded_cost_lower_usd'])
        self.assertFalse(summary['model_cost_coverage_complete'])


if __name__ == '__main__':
    unittest.main()
