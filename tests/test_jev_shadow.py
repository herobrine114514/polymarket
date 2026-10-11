from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from experiments import jev_gateway_probe as gateway
from experiments import jev_btc5m_shadow as shadow


class Response:
    status = 200

    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class JevTests(unittest.TestCase):
    def setUp(self):
        self.questions = {"up": {"type": "noul", "instructions": "Will the event resolve Up?"}}
        self.body = gateway.request_body({"public": "fixture"}, self.questions)
        self.raw = {"model": gateway.MODEL, "answers": {"up": {"type": "noul", "noul": 0.3}},
                    "usage": {"input_tokens": 30, "output_tokens": 10}}

    def test_question_identity_and_probability_validation(self):
        for change in ["missing", "extra", "boolean", "nan", "range", "wrong_type", "wrong_model"]:
            raw = json.loads(json.dumps(self.raw))
            if change == "missing": raw["answers"] = {}
            if change == "extra": raw["answers"]["another"] = {"type": "noul", "noul": .5}
            if change == "boolean": raw["answers"]["up"]["noul"] = True
            if change == "nan": raw["answers"]["up"]["noul"] = float("nan")
            if change == "range": raw["answers"]["up"]["noul"] = 1.1
            if change == "wrong_type": raw["answers"]["up"]["type"] = "score"
            if change == "wrong_model": raw["model"] = "another-model"
            with self.subTest(change=change), self.assertRaises(ValueError):
                gateway.parse_response(raw, self.questions)

    def test_request_cannot_add_tools_paid_model_or_unbounded_timeout(self):
        for body in [{**self.body, "model": "jev-1.13"}, {**self.body, "tools": []}]:
            with self.assertRaises(ValueError):
                gateway.evaluate_api(body, Path("unused"))
        for timeout in [0, 16]:
            with self.assertRaises(ValueError):
                gateway.evaluate_api(self.body, Path("unused"), timeout_seconds=timeout)
        for questions in [[], {}, {1: self.questions["up"]}, {"x": {"type": "noul", "instructions": ""}},
                          {"x": {"type": "choice", "instructions": "Choose"}}]:
            with self.assertRaises(ValueError):
                gateway.request_body("fixture", questions)

    def test_valid_api_response_keeps_auth_out_of_receipt(self):
        calls = []
        def transport(request, timeout):
            calls.append(request)
            self.assertEqual(request.full_url, gateway.ENDPOINT)
            self.assertEqual(request.get_method(), "POST")
            return Response(self.raw)
        with patch.object(gateway, "credential", return_value="synthetic-secret-not-real"):
            report = gateway.evaluate_api(self.body, Path("unused"), open_request=transport)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["answers"], {"up": .3})
        self.assertEqual(report["usage"], {"input_tokens": 30, "output_tokens": 10})
        self.assertFalse(report["production_database_written"])
        self.assertNotIn("synthetic-secret", json.dumps(report))
        self.assertNotIn("Authorization", json.dumps(report))

    def test_schema_failure_still_preserves_received_usage(self):
        self.raw["answers"]["up"]["noul"] = 2
        with patch.object(gateway, "credential", return_value="synthetic-secret"):
            report = gateway.evaluate_api(self.body, Path("unused"), open_request=lambda *a, **k: Response(self.raw))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["received_usage"], {"input_tokens": 30, "output_tokens": 10})
        self.assertNotIn("answers", report)

    def test_invalid_usage_is_unknown_not_zero(self):
        self.raw["usage"]["input_tokens"] = True
        report = gateway.parse_response(self.raw, self.questions)
        self.assertEqual(report["usage_status"], "unknown")
        self.assertIsNone(report["usage"])

    def test_api_failures_are_not_retried_or_echoed(self):
        for failure in [HTTPError(gateway.ENDPOINT, 429, "synthetic-secret", {}, None),
                        TimeoutError("synthetic-secret")]:
            calls = []
            def transport(*args, **kwargs):
                calls.append(1)
                raise failure
            with patch.object(gateway, "credential", return_value="synthetic-secret"):
                report = gateway.evaluate_api(self.body, Path("unused"), open_request=transport)
            self.assertEqual(len(calls), 1)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["usage_status"], "unknown")
            self.assertNotIn("synthetic-secret", json.dumps(report))

    def test_redirect_is_blocked(self):
        with self.assertRaises(HTTPError):
            gateway.NoRedirect().redirect_request(type("R", (), {"full_url": gateway.ENDPOINT})(),
                None, 302, "redirect", {}, "https://another.example/")

    def payload(self):
        now = gateway.utcnow()
        return {"market_id": "fixture", "as_of": now.isoformat(), "end_at": (now + timedelta(minutes=3)).isoformat(),
                "question": "BTC up?", "resolution_rules": "fixture", "window_timing_utc": {},
                "gamma_crypto_config": {}, "evidence": [{"id": 1, "source": "fixture", "title": "price",
                "summary": "public observation", "published_at": (now-timedelta(seconds=1)).isoformat(),
                "first_seen_at": (now-timedelta(seconds=1)).isoformat()}]}

    def test_state_excludes_parent_forecast_market_mid_and_provider_settings(self):
        payload = self.payload()
        payload.update(provider="deepseek", api_key="synthetic-secret", p_yes=.99, market_mid=.98, review={})
        state = shadow.frozen_state(payload)
        self.assertEqual(state["evidence"][0]["summary"], "public observation")
        for key in ["provider", "api_key", "p_yes", "market_mid", "review"]:
            self.assertNotIn(key, state)
        self.assertNotIn("synthetic-secret", json.dumps(state))

    def test_future_or_unavailable_evidence_is_rejected(self):
        for key in ["published_at", "first_seen_at"]:
            payload = self.payload()
            payload["evidence"][0][key] = (gateway.utcnow()+timedelta(minutes=1)).isoformat()
            with self.assertRaises(ValueError): shadow.frozen_state(payload)

    def test_stale_future_asof_abstention_and_near_deadline_are_ineligible(self):
        now = gateway.utcnow()
        target = {"analysis_abstain": False, "as_of": (now-timedelta(seconds=2)).isoformat(),
                  "end_at": (now+timedelta(minutes=2)).isoformat()}
        self.assertTrue(shadow.eligible(target, now))
        for modified in [{**target, "as_of": (now-timedelta(seconds=31)).isoformat()},
                         {**target, "as_of": (now+timedelta(seconds=1)).isoformat()},
                         {**target, "analysis_abstain": True},
                         {**target, "end_at": (now+timedelta(seconds=59)).isoformat()}]:
            self.assertFalse(shadow.eligible(modified, now))

    def test_simultaneous_targets_cannot_exceed_reserved_call_budget(self):
        now = gateway.utcnow()
        as_of, end = (now-timedelta(seconds=1)).isoformat(), (now+timedelta(minutes=2)).isoformat()
        rows = [{"id": i, "as_of": as_of, "payload_json": "{}"} for i in range(4)]
        calls = []
        class Cursor:
            def fetchone(self): return None
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, query, args): return rows if query == shadow.TARGET_SQL else Cursor()
        def target(row, *args):
            return {"event_id": str(row["id"]), "market_id": str(row["id"]), "prediction_id": row["id"],
                    "as_of": as_of, "end_at": end, "analysis_abstain": False, "payload_sha256": "fixture"}
        def api(*args, **kwargs):
            calls.append(1)
            return {"status": "completed", "completed_at": gateway.utcnow().isoformat(),
                    "answers": {"up": .5, "unclear_direction": .9, "unverified_opening_reference": .99}}
        with tempfile.TemporaryDirectory() as directory:
            argv = ["shadow", "--output", directory, "--maximum-calls", "2", "--watch-seconds", "1"]
            with patch.object(sys_module := shadow.sys, "argv", argv), patch.object(shadow, "sources", return_value={}), \
                 patch.object(shadow, "connect_readonly", return_value=Connection()), \
                 patch.object(shadow, "target_record", side_effect=target), \
                 patch.object(shadow, "frozen_state", return_value={}), \
                 patch.object(shadow, "evaluate_api", side_effect=api), redirect_stdout(io.StringIO()):
                shadow.main()
            report = json.loads((Path(directory)/"status.json").read_text())
        self.assertEqual(len(calls), 2)
        self.assertEqual(report["calls"], 2)
        self.assertEqual(report["timely_nonabstaining"], 0)


if __name__ == "__main__":
    unittest.main()
