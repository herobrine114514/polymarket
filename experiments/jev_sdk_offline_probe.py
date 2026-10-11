"""Verify pinned TypeSafe SDK against OpenCode's wire format with mock transport only."""
from __future__ import annotations

import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path

import httpx2
from typesafe_sdk import Noul, RetryPolicy, TypeSafeClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.jev_gateway_probe import ENDPOINT, MODEL, parse_response, request_body, utcnow
from experiments.calibration_live_shadow import write_report


def main():
    if version("typesafe-sdk") != "0.7.2":
        raise ValueError("SDK version changed")
    requests, checks = [], []
    raw = {"model": MODEL, "answers": {"up": {"type": "noul", "noul": .3}},
           "usage": {"input_tokens": 301, "output_tokens": 23}}
    state = {"public": "Synthetic fixture; no forecast credit"}
    expected = request_body(state, {"up": {"type": "noul", "instructions": "Will the event resolve Up?"}})
    def record(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append({"check": name, "passed": True})
    def handler(request):
        if str(request.url) != ENDPOINT or request.method != "POST":
            raise ValueError("Non-gateway fixture request blocked")
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=raw)
    options = {"api_key": "synthetic-offline-test-key", "base_url": "https://opencode.ai/zen",
               "model": MODEL, "timeout": 12, "retry": RetryPolicy(max_retries=0)}
    with TypeSafeClient(**options, transport=httpx2.MockTransport(handler)) as client:
        response = client.system_one(state, {"up": Noul(instructions="Will the event resolve Up?")})
    wire = requests[-1]
    record(wire["model"] == MODEL and wire["state"] == expected["state"], "gateway path and pinned model/state")
    record(wire["questions"]["up"]["type"] == "noul" and
           wire["questions"]["up"]["instructions"] == expected["questions"]["up"]["instructions"],
           "same typed question identity and semantics")
    record(response.answers["up"].noul == .3, "SDK structured answer")
    record(response.usage.input_tokens == 301 and response.usage.output_tokens == 23, "SDK measured usage")
    record(parse_response(raw, expected["questions"])["answers"]["up"] == .3, "application parses same SDK-compatible response")
    failure_calls = []
    def failure_handler(request):
        if str(request.url) != ENDPOINT:
            raise ValueError("Non-gateway fixture request blocked")
        failure_calls.append(1)
        return httpx2.Response(429, json={"error": "synthetic rate limit"})
    caught = False
    try:
        with TypeSafeClient(**options, transport=httpx2.MockTransport(failure_handler)) as client:
            client.system_one(state, {"up": Noul(instructions="Will the event resolve Up?")})
    except Exception:
        caught = True
    record(caught and len(failure_calls) == 1, "explicit no-retry policy sends only one attempt")
    invalid = {**raw, "answers": {"up": {"type": "noul", "noul": 2}}}
    rejected = False
    try:
        parse_response(invalid, expected["questions"])
    except ValueError:
        rejected = True
    record(rejected, "application rejects out-of-range answers independently of SDK")
    changed_model = False
    try:
        # The gateway adapter permits only the registered free-model request.
        from experiments.jev_gateway_probe import evaluate_api
        evaluate_api({**expected, "model": "jev-1.13"}, ROOT / ".polycast")
    except ValueError:
        changed_model = True
    record(changed_model, "application rejects paid-model substitution before loading any credential")
    report = {"checked_at": utcnow().isoformat(), "sdk": "typesafe-sdk==0.7.2", "scope":
              "In-memory SDK transport compatibility; no user credential, network or forecast credit",
              "checks": checks, "passed": len(checks), "fixture_requests": len(requests)+len(failure_calls),
              "external_requests": 0, "user_credentials_loaded": False,
              "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "dependency_freeze_sha256": hashlib.sha256((ROOT / ".polycast/oss-jev-sdk-freeze-20261003.json").read_bytes()).hexdigest()}
    write_report(ROOT / ".polycast/jev-sdk-offline-probe-20261003.json", report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
