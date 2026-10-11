"""Bounded, budgeted DeepSeek thinking comparison on frozen live BTC inputs.

Only model usage is added to the production ledger. Predictions, decisions and
orders are never modified. No reasoning text, API key or request headers saved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from polycast import analysis
from polycast.billing import capture_usage, save_model_usage
from polycast.config import Config
from polycast.analysis import ProviderUnavailable
from polycast.store import database, reserve_model_call, finish_model_call
from experiments.calibration_live_shadow import (TARGET_SQL, connect_readonly,
    target_record, write_once, fingerprint, source_hashes, write_report)
from polycast.market import parse_utc

EXPERIMENT = "btc5m-v4pro-thinking-high-shadow-v1"
PARAMETERS = {"thinking": {"type": "enabled"}, "reasoning_effort": "high",
              "max_tokens": 8000, "timeout_seconds": 75, "maximum_calls": 2,
              "minimum_remaining_seconds": 110, "poll_seconds": 5}


def utcnow():
    return datetime.now(timezone.utc)


def research_request(data):
    body = json.loads(data)
    if (body.get("model") != "deepseek-v4-pro" or body.get("stream") is not False or
            body.get("response_format") != {"type": "json_object"} or "tools" in body):
        raise ValueError("Unexpected research request")
    body.update(thinking=PARAMETERS["thinking"], reasoning_effort=PARAMETERS["reasoning_effort"],
                max_tokens=PARAMETERS["max_tokens"])
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def sources():
    return {**source_hashes(ROOT), "experiments/deepseek_thinking_shadow.py":
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def call(config, target, payload, report):
    with database(config.database) as conn:
        call_id = reserve_model_call(conn, target["market_id"], "analysis", "deepseek-v4-pro",
                                     config.model_call_limit("deepseek"), "deepseek")
    report["model_call_id"] = call_id
    receipt = {}
    original_request = analysis.Request

    def rewritten(url, data=None, headers=None, method=None, **kwargs):
        if url != "https://api.deepseek.com/chat/completions" or method != "POST":
            raise ValueError("Unexpected research endpoint")
        encoded = research_request(data)
        body = json.loads(encoded)
        report["messages_sha256"] = fingerprint(body["messages"])
        report["effective_request"] = {k: body[k] for k in ["model", "thinking", "reasoning_effort", "max_tokens", "stream", "response_format"]}
        return original_request(url, data=encoded, headers=headers, method=method, **kwargs)

    def usage(item):
        receipt.update(item)
        with database(config.database) as conn:
            save_model_usage(conn, call_id, item)

    started = time.monotonic()
    try:
        with capture_usage(usage), patch.object(analysis, "Request", rewritten):
            result = analysis.run_codex(payload, timeout_seconds=PARAMETERS["timeout_seconds"], data_dir=config.data_dir)
        with database(config.database) as conn:
            finish_model_call(conn, call_id, provider="deepseek")
        report["result"] = {k: result[k] for k in ["market_id", "as_of", "p_yes", "abstain"]}
        report["schema_and_evidence_ids_validated"] = True
        report["status"] = "completed"
    except Exception as exc:
        with database(config.database) as conn:
            pause = exc.retry_at.isoformat() if isinstance(exc, ProviderUnavailable) else None
            finish_model_call(conn, call_id, f"{type(exc).__name__}: thinking research failed", pause, "deepseek")
        report.update(status="failed", failure_type=type(exc).__name__)
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        report["completed_at"] = utcnow().isoformat()
        report["usage"] = {k: receipt.get(k) for k in ["usage_status", "returned_model", "prompt_tokens",
                            "completion_tokens", "total_tokens", "reasoning_tokens", "cost_lower_usd", "cost_upper_usd"]}
    return report


def run(config, output, duration):
    manifest = {"experiment": EXPERIMENT, "registered_at": utcnow().isoformat(),
                "model_version": "deepseek-v4-pro", "prompt_version": analysis.BTC5M_PROMPT_VERSION,
                "parameters": PARAMETERS, "source_hashes": sources(),
                "scope": "Research only: two calls maximum, no production predictions/reviews/orders",
                "comparison": "Identical frozen evidence and production prompt; only thinking/effort/output cap changes; original market baseline before as_of"}
    if not write_once(output / "manifest.json", manifest):
        raise ValueError("Already registered; no automatic rerun/resubmission")
    until = time.monotonic() + duration
    requests, completed = [], []
    while time.monotonic() < until and len(requests) < PARAMETERS["maximum_calls"]:
        if sources() != manifest["source_hashes"]:
            raise ValueError("Registered source changed")
        now = utcnow()
        with connect_readonly(config.database) as conn:
            conn.execute("BEGIN")
            rows = [dict(r) for r in conn.execute(TARGET_SQL, (manifest["registered_at"],))]
        rows.sort(key=lambda r: (r["as_of"], r["id"]))
        for row in rows:
            target = target_record(row, manifest, now)
            if not target or now + timedelta(seconds=PARAMETERS["minimum_remaining_seconds"]) >= parse_utc(target["end_at"]):
                continue
            event_file = hashlib.sha256(target["event_id"].encode()).hexdigest() + ".json"
            intent = {"event_id": target["event_id"], "prediction_id": target["prediction_id"],
                      "payload_sha256": target["payload_sha256"], "started_at": utcnow().isoformat(),
                      "scope": "Model research call; never automatically retry"}
            if not write_once(output / "call-intents" / event_file, intent):
                continue
            requests.append(intent)
            payload = json.loads(row["payload_json"])
            payload["provider"] = "deepseek"
            report = {"experiment": EXPERIMENT, "manifest_sha256": fingerprint(manifest),
                      "started_at": intent["started_at"], "baseline": target,
                      "parameters": PARAMETERS, "billing_ledger_written": True,
                      "production_prediction_written": False, "orders_submitted": 0,
                      "independent_review_run": False, "reasoning_text_saved": False}
            report = call(config, target, payload, report)
            finished = utcnow()
            with connect_readonly(config.database) as conn:
                outcome_seen = conn.execute("SELECT 1 FROM resolutions WHERE market_id=?", (target["market_id"],)).fetchone()
            report["finished_before_event_end"] = finished < parse_utc(target["end_at"]) and not outcome_seen
            report["record_sha256"] = fingerprint(report)
            if write_once(output / "responses" / event_file, report):
                write_once(output / "receipts" / event_file, {"published_at": utcnow().isoformat(),
                    "record_sha256": report["record_sha256"], "manifest_sha256": fingerprint(manifest)})
            completed.append(report)
            write_report(output / "status.json", {"status": "observing", "checked_at": utcnow().isoformat(),
                         "calls": len(requests), "responses": len(completed), "maximum_calls": PARAMETERS["maximum_calls"]})
            print(json.dumps({k: report.get(k) for k in ["started_at", "completed_at", "status", "model_call_id",
                             "elapsed_seconds", "finished_before_event_end", "result", "usage", "failure_type"]}), flush=True)
            if len(requests) >= PARAMETERS["maximum_calls"]:
                break
        time.sleep(min(PARAMETERS["poll_seconds"], max(0, until - time.monotonic())))
    status = {"experiment": EXPERIMENT, "status": "bounded_observation_completed",
              "checked_at": utcnow().isoformat(), "calls": len(requests), "responses": len(completed),
              "completed": sum(r["status"] == "completed" for r in completed),
              "before_end": sum(r["finished_before_event_end"] for r in completed), "orders_submitted": 0}
    write_report(output / "status.json", status)
    print(json.dumps(status), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".polycast/thinking-high-shadow-20261003")
    parser.add_argument("--watch-seconds", type=int, default=620)
    args = parser.parse_args()
    if not 10 <= args.watch_seconds <= 1800:
        parser.error("Observation must be bounded between 10 and 1800 seconds")
    run(Config(data_dir=ROOT / ".polycast"), args.output, args.watch_seconds)
