import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from experiments.deepseek_thinking_shadow import research_request, call
from experiments.calibration_live_shadow import write_once
from polycast import analysis
from polycast.billing import emit_deepseek_usage
from polycast.config import Config


class ThinkingShadowTests(unittest.TestCase):
    def request(self):
        return {"model": "deepseek-v4-pro", "stream": False,
                "thinking": {"type": "disabled"}, "max_tokens": 3000,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": "Frozen JSON test input"}]}

    def test_only_reasoning_settings_change(self):
        before = self.request()
        after = json.loads(research_request(json.dumps(before).encode()))
        self.assertEqual(after['messages'], before['messages'])
        self.assertEqual(after['response_format'], before['response_format'])
        self.assertEqual(after['thinking'], {'type': 'enabled'})
        self.assertEqual(after['reasoning_effort'], 'high')
        self.assertEqual(after['max_tokens'], 8000)

    def test_other_models_tools_and_streaming_are_rejected(self):
        for key, value in [('model', 'other'), ('tools', []), ('stream', True)]:
            before = self.request()
            before[key] = value
            with self.assertRaises(ValueError):
                research_request(json.dumps(before).encode())

    def exercise_call(self, fail=False, usage=True):
        @contextmanager
        def fake_database(*_):
            yield object()
        saved = []
        def remote(payload, timeout_seconds, data_dir):
            request = analysis.Request('https://api.deepseek.com/chat/completions',
                data=json.dumps(self.request()).encode(), headers={'Authorization': 'Bearer TEST-ONLY'}, method='POST')
            self.assertEqual(json.loads(request.data)['thinking'], {'type': 'enabled'})
            self.assertEqual(timeout_seconds, 75)
            if usage:
                emit_deepseek_usage({'model': 'DeepSeek-V4-Pro-0813', 'usage': {
                    'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140,
                    'prompt_cache_hit_tokens': 20, 'prompt_cache_miss_tokens': 80,
                    'completion_tokens_details': {'reasoning_tokens': 30}}}, 'deepseek-v4-pro')
            if fail:
                raise RuntimeError('Fixture schema failure')
            return {'market_id': 'fixture', 'as_of': 'time', 'p_yes': .6, 'abstain': False}
        with patch('experiments.deepseek_thinking_shadow.database', fake_database), \
                patch('experiments.deepseek_thinking_shadow.reserve_model_call', return_value=7) as reserve, \
                patch('experiments.deepseek_thinking_shadow.finish_model_call') as finish, \
                patch('experiments.deepseek_thinking_shadow.save_model_usage', side_effect=lambda _, cid, item: saved.append((cid, item))), \
                patch.object(analysis, 'run_codex', remote):
            result = call(Config(data_dir=Path('unused')), {'market_id': 'fixture'}, {'provider': 'deepseek'}, {})
        self.assertEqual(reserve.call_count, 1)
        self.assertEqual(finish.call_count, 1)
        self.assertNotIn('TEST-ONLY', json.dumps(result))
        self.assertNotIn('Authorization', json.dumps(result))
        return result, saved

    def test_usage_belongs_to_reserved_call_and_reasoning_is_not_double_counted(self):
        result, saved = self.exercise_call()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(saved[0][0], 7)
        self.assertEqual(result['usage']['total_tokens'], 140)
        self.assertEqual(result['usage']['completion_tokens'], 40)
        self.assertEqual(result['usage']['reasoning_tokens'], 30)

    def test_schema_failure_keeps_billable_response_usage(self):
        result, saved = self.exercise_call(fail=True)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['usage']['total_tokens'], 140)
        self.assertEqual(len(saved), 1)

    def test_timeout_without_usage_remains_unknown(self):
        result, saved = self.exercise_call(fail=True, usage=False)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['usage']['completion_tokens'])
        self.assertIsNone(result['usage']['cost_lower_usd'])
        self.assertEqual(saved, [])

    def test_existing_call_intent_cannot_be_silently_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'call-intent.json'
            self.assertTrue(write_once(path, {'event': 'fixture'}))
            self.assertFalse(write_once(path, {'event': 'fixture'}))


if __name__ == '__main__':
    unittest.main()
