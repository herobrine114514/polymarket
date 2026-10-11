"""Opt-in, bounded research receipts for completions before JSON/schema checks.

No credentials, headers, provider request or arbitrary response metadata are
stored. The normal runtime does not retain output unless a sink is bound.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import math

VERSION = 'raw-model-content-receipt-v1'
MAX_BYTES = 128 * 1024
_sink = ContextVar('polycast_model_output_sink', default=None)


@contextmanager
def capture_model_output(sink):
    token = _sink.set(sink)
    try: yield
    finally: _sink.reset(token)


def strict_json_object(content: str) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError('Duplicate JSON object key')
            value[key] = item
        return value
    def constant(_): raise ValueError('Non-finite JSON constant')
    def finite_float(text):
        value = float(text)
        if not math.isfinite(value): raise ValueError('Overflowing JSON number')
        return value
    value = json.loads(content, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)
    if not isinstance(value, dict): raise ValueError('Completion must be one JSON object')
    return value


def emit_model_output(content, *, provider: str, model: str, finish_reason=None):
    sink = _sink.get()
    if sink is None: return
    if provider not in ('deepseek', 'codex'): raise ValueError('Unknown research output provider')
    try: blob = content.encode('utf-8') if isinstance(content, str) else None
    except UnicodeEncodeError: blob = None
    complete = blob is not None and len(blob) <= MAX_BYTES
    raw = content if complete else None
    error = None
    if not complete: error = 'missing_nontext_or_oversize_content'
    elif finish_reason == 'length': error = 'truncated_completion'
    else:
        try: strict_json_object(raw)
        except (ValueError, TypeError, RecursionError): error = 'invalid_duplicate_or_nonfinite_json_object'
    sink({'protocol': VERSION, 'received_at': datetime.now(timezone.utc).isoformat(),
          'provider': provider, 'requested_model': model,
          'finish_reason': finish_reason if finish_reason in ('stop', 'length', 'content_filter', None) else 'other',
          'content_sha256': hashlib.sha256(blob).hexdigest() if blob is not None else None,
          'content_bytes': len(blob) if blob is not None else None, 'content_complete': complete,
          'raw_content': raw, 'strict_json_object_valid': error is None, 'error': error})
