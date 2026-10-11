from __future__ import annotations
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from polycast.analysis import _run_deepseek
from polycast.model_output import MAX_BYTES, capture_model_output, emit_model_output, strict_json_object


class ModelOutputTests(unittest.TestCase):
    def test_strict_parser_rejects_nested_duplicates_nonfinite_and_nonobjects(self):
        for raw in ('{"a":1,"a":2}', '{"a":{"x":1,"x":2}}', '{"a":NaN}', '{"a":Infinity}',
                    '{"a":1e999}', '[]', 'null', 'not json'):
            with self.subTest(raw=raw), self.assertRaises(ValueError): strict_json_object(raw)
        self.assertEqual(strict_json_object('{"a":1,"nested":{"a":2}}'), {'a':1,'nested':{'a':2}})

    def test_exact_content_and_hash_survive_invalid_json(self):
        raw=' {"a":1,"a":2}\n'; received=[]
        with capture_model_output(received.append): emit_model_output(raw,provider='deepseek',model='deepseek-v4-pro')
        self.assertEqual(received[0]['raw_content'],raw)
        self.assertEqual(received[0]['content_sha256'],hashlib.sha256(raw.encode()).hexdigest())
        self.assertFalse(received[0]['strict_json_object_valid'])

    def test_capture_is_bounded_and_truncated_valid_json_is_rejected(self):
        for raw,finish in (('x'*(MAX_BYTES+1),None), ('{}','length'), (None,None), ('\ud800',None)):
            received=[]
            with capture_model_output(received.append): emit_model_output(raw,provider='deepseek',model='deepseek-v4-pro',finish_reason=finish)
            self.assertFalse(received[0]['strict_json_object_valid'])
            if raw=='{}': self.assertEqual(received[0]['raw_content'],raw)
            else: self.assertIsNone(received[0]['raw_content'])

    def test_nested_sinks_restore_outer_and_do_not_leak_after_exception(self):
        outer=[];inner=[]
        with capture_model_output(outer.append):
            emit_model_output('{}',provider='codex',model='test')
            try:
                with capture_model_output(inner.append):
                    emit_model_output('{}',provider='codex',model='test'); raise RuntimeError()
            except RuntimeError: pass
            emit_model_output('{}',provider='codex',model='test')
        emit_model_output('{}',provider='codex',model='test')
        self.assertEqual(len(outer),2);self.assertEqual(len(inner),1)

    def test_billable_invalid_schema_still_has_usage_and_raw_output_receipt(self):
        content='{"wrong":"exact original"}'
        payload={'choices':[{'finish_reason':'stop','message':{'content':content}}],
                 'model':'deepseek-v4-pro','usage':{},'untrusted_extra':'not copied'}
        received=[]
        with patch('polycast.analysis.deepseek_key',return_value='test-key'), \
             patch('polycast.analysis.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())), \
             patch('polycast.analysis.emit_deepseek_usage') as usage, capture_model_output(received.append):
            with self.assertRaisesRegex(RuntimeError, 'schema'):
                _run_deepseek('test',{'type':'object','required':['value'],'properties':{'value':{'type':'number'}},'additionalProperties':False},5,None)
        usage.assert_called_once(); self.assertEqual(received[0]['raw_content'],content)
        self.assertNotIn('test-key',str(received));self.assertNotIn('untrusted_extra',str(received))


if __name__ == '__main__': unittest.main()
